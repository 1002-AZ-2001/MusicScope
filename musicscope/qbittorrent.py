"""Fichiers gérés par qBittorrent, lus par sa Web API (v2), en lecture seule.

MusicScope n'envoie à qBittorrent que des requêtes de lecture : connexion, version,
liste des torrents et de leurs fichiers. Il ne met jamais un torrent en pause, ne le
supprime pas et ne change aucun réglage.

Documentation : https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0)
et WebAPI_Changelog.md (401 sur identifiants refusés depuis la WebAPI 2.14.0, clé API
``Authorization: Bearer qbt_…`` depuis qBittorrent 5.2.0).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from http.client import HTTPException
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from .cache import check_cancel
from .jellyfin import PathMapping, map_server_path

# Torrent terminé et partagé (ou prêt à l'être) : ses fichiers ne doivent pas bouger.
SEEDING_STATES = {"uploading", "stalledUP", "queuedUP", "forcedUP", "checkingUP"}
# En pause : qBittorrent relira les fichiers à la reprise ; les déplacer casse le torrent.
PAUSED_STATES = {"pausedUP", "stoppedUP"}
MAX_RESPONSE = 64 * 1024 * 1024


class QBittorrentUnavailable(RuntimeError):
    pass


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme, (parsed.hostname or "").casefold(), parsed.port


class _SameOriginRedirect(HTTPRedirectHandler):
    """Ni le cookie de session ni la clé ne doivent suivre une redirection vers un autre hôte."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != _origin(req.full_url):
            raise QBittorrentUnavailable("Redirection qBittorrent vers une autre origine refusée.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


@dataclass(frozen=True)
class TorrentFile:
    torrent: str
    state: str
    server_path: str
    local_path: str | None
    size: int

    @property
    def active(self) -> bool:
        return self.state in SEEDING_STATES


@dataclass
class SeedIndex:
    """Fichiers de qBittorrent, retrouvés dans la médiathèque par chemin, par identité
    physique (lien Lidarr) ou, à défaut d'identité fiable, par nom et taille."""
    files: list[TorrentFile] = field(default_factory=list)
    torrents: int = 0
    unmapped: int = 0
    version: str = ""
    by_path: dict[str, TorrentFile] = field(default_factory=dict)
    by_identity: dict[tuple[int, int], TorrentFile] = field(default_factory=dict)
    by_name_size: dict[tuple[str, int], TorrentFile] = field(default_factory=dict)

    def find(self, path: str, size: int | None = None) -> tuple[TorrentFile | None, str]:
        """(fichier de torrent, certitude) : « chemin », « identité », « nom et taille » ou ""."""
        known = self.by_path.get(os.path.normcase(os.path.abspath(path)))
        if known is not None:
            return known, "chemin"
        try:
            info = os.stat(path, follow_symlinks=False)
        except OSError:
            info = None
        if info is not None and info.st_ino:
            known = self.by_identity.get((info.st_dev, info.st_ino))
            if known is not None:
                return known, "identité"
        size = info.st_size if info is not None else size
        if size is not None:
            known = self.by_name_size.get((os.path.basename(path).casefold(), size))
            if known is not None:
                return known, "nom et taille"
        return None, ""

    def protected(self, path: str, size: int | None = None) -> TorrentFile | None:
        """Fichier qu'il ne faut ni déplacer ni réécrire : c'est le fichier même d'un torrent
        (même chemin ou même inode, lien Lidarr). Une simple copie de même nom et de même
        taille ne compte pas : la déplacer ne touche pas le torrent."""
        known, certainty = self.find(path, size)
        return known if certainty in {"chemin", "identité"} else None

    def summary(self) -> str:
        active = sum(item.active for item in self.files)
        return (f"qBittorrent {self.version or '?'} : {self.torrents} torrent(s), {len(self.files)} fichier(s) "
                f"dont {active} en partage actif" + (f", {self.unmapped} chemin(s) sans correspondance locale"
                                                    if self.unmapped else ""))


class QBittorrentClient:
    def __init__(self, server_url: str, username: str = "", password: str = "", api_key: str = "",
                 opener=urlopen, page_size: int = 500):
        parsed = urlsplit(server_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ValueError("Adresse qBittorrent HTTP(S) sans identifiants, paramètres ni fragment requise.")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("Port qBittorrent invalide.") from exc
        api_key = api_key.strip()
        if api_key and not re.fullmatch(r"[\x21-\x7e]{8,256}", api_key):
            raise ValueError("Clé API qBittorrent invalide : recopiez-la (Préférences → WebUI → API Key).")
        if not api_key and not username:
            raise ValueError("Indiquez une clé API (qBittorrent ≥ 5.2) ou un identifiant et un mot de passe.")
        if any(character in value for value in (username, password) for character in "\r\n\0"):
            raise ValueError("Identifiant ou mot de passe qBittorrent invalide.")
        self.server_url = server_url.rstrip("/") + "/"
        self._origin = f"{parsed.scheme}://{parsed.netloc}"
        self._username, self._password, self._api_key = username, password, api_key
        self._sid = ""
        self.opener = build_opener(_SameOriginRedirect()).open if opener is urlopen else opener
        self.page_size = page_size
        self.timeout = 20

    def _request(self, path: str, query: dict | None = None, data: dict | None = None, raw: bool = False):
        url = urljoin(self.server_url, path) + ("?" + urlencode(query) if query else "")
        # Protection CSRF de qBittorrent : Referer et Origin identiques à l'hôte interrogé.
        headers = {"Accept": "application/json", "Referer": self._origin, "Origin": self._origin,
                   "User-Agent": "MusicScope"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        elif self._sid:
            headers["Cookie"] = f"SID={self._sid}"
        body = urlencode(data).encode() if data is not None else None
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
        try:
            with self.opener(request, timeout=self.timeout) as response:
                content = response.read(MAX_RESPONSE + 1)
                cookies = response.headers.get_all("Set-Cookie") if hasattr(response, "headers") \
                    and response.headers is not None else None
            if len(content) > MAX_RESPONSE:
                raise QBittorrentUnavailable("Réponse qBittorrent trop volumineuse.")
        except HTTPError as exc:
            if exc.code == 401:
                raise QBittorrentUnavailable("qBittorrent refuse l’accès (HTTP 401) : identifiant, mot de passe "
                                             "ou clé API incorrects.") from exc
            if exc.code == 403:
                raise QBittorrentUnavailable("qBittorrent refuse l’accès (HTTP 403) : session expirée, en-têtes "
                                             "refusés, ou adresse bannie après trop d’échecs de connexion.") from exc
            raise QBittorrentUnavailable(f"qBittorrent indisponible (HTTP {exc.code}).") from exc
        except (URLError, OSError, TimeoutError, HTTPException) as exc:
            raise QBittorrentUnavailable("Connexion qBittorrent impossible : vérifiez l’adresse, le port et le réseau.") from exc
        if raw:
            return content, cookies or []
        try:
            return json.loads(content)
        except (ValueError, UnicodeError) as exc:
            raise QBittorrentUnavailable("Réponse qBittorrent illisible : l’adresse désigne-t-elle bien la WebUI ?") from exc

    def login(self) -> None:
        if self._api_key or self._sid:
            return
        content, cookies = self._request("api/v2/auth/login",
                                         data={"username": self._username, "password": self._password}, raw=True)
        text = content.decode("utf-8", "replace").strip()
        sid = ""
        for cookie in cookies:
            match = re.match(r"\s*SID=([^;\s]+)", cookie)
            if match:
                sid = match.group(1)
        if text.startswith("Fails") or not sid:
            raise QBittorrentUnavailable("qBittorrent refuse la connexion : identifiant ou mot de passe incorrect.")
        self._sid = sid

    def version(self) -> str:
        self.login()
        content, _ = self._request("api/v2/app/version", raw=True)
        return content.decode("utf-8", "replace").strip()[:40]

    def torrents(self, cancel=None) -> list[dict]:
        self.login()
        result, offset = [], 0
        while True:
            check_cancel(cancel)
            page = self._request("api/v2/torrents/info", {"filter": "all", "includeFiles": "true",
                                                          "limit": self.page_size, "offset": offset})
            if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
                raise QBittorrentUnavailable("Liste des torrents illisible.")
            result.extend(page)
            if len(page) < self.page_size:
                return result
            offset += len(page)
            if offset > 1_000_000:
                raise QBittorrentUnavailable("Liste des torrents anormalement longue.")

    def files(self, torrent_hash: str) -> list[dict]:
        if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", torrent_hash):
            raise QBittorrentUnavailable("Identifiant de torrent invalide.")
        files = self._request("api/v2/torrents/files", {"hash": torrent_hash})
        if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
            raise QBittorrentUnavailable("Liste des fichiers du torrent illisible.")
        return files

    def seed_index(self, mappings: list[PathMapping], cancel=None, progress=None) -> SeedIndex:
        index = SeedIndex(version=self.version())
        torrents = self.torrents(cancel)
        index.torrents = len(torrents)
        resolved: dict[str, Path] = {}
        for position, torrent in enumerate(torrents, 1):
            check_cancel(cancel)
            state = str(torrent.get("state", ""))
            save_path = str(torrent.get("save_path") or "")
            content_path = str(torrent.get("content_path") or "")
            files = torrent.get("files")
            if not isinstance(files, list):  # WebAPI antérieure à 2.11.8 : une requête par torrent
                files = self.files(str(torrent.get("hash", "")))
            for item in files:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    continue
                size = item.get("size") if isinstance(item.get("size"), int) else -1
                server = _join(save_path, item["name"])
                local = _local(server, content_path, item["name"], mappings, resolved)
                entry = TorrentFile(str(torrent.get("name", "")), state, server, local, size)
                index.files.append(entry)
                if local is None:
                    index.unmapped += 1
                    continue
                index.by_path[os.path.normcase(os.path.abspath(local))] = entry
                try:
                    info = os.stat(local, follow_symlinks=False)
                except OSError:
                    info = None
                if info is not None and info.st_ino:
                    index.by_identity[(info.st_dev, info.st_ino)] = entry
                if size >= 0:
                    index.by_name_size[(PurePosixPath(item["name"].replace("\\", "/")).name.casefold(), size)] = entry
            if progress:
                progress(position, len(torrents), f"qBittorrent : {torrent.get('name', '')}")
        return index


def _join(base: str, name: str) -> str:
    windows = bool(re.match(r"^[a-zA-Z]:|^\\\\", base))
    return str((PureWindowsPath if windows else PurePosixPath)(base, name))


def _local(server: str, content_path: str, name: str, mappings, resolved) -> str | None:
    """Chemin local du fichier : correspondance explicite, sinon chemin identique (même machine)."""
    candidates = [server]
    # Torrent multi-fichiers sans dossier racine dans `name` : rattacher au chemin du contenu.
    if content_path and not server.startswith(content_path):
        candidates.append(_join(content_path, name))
    for candidate in candidates:
        if mappings:
            target, _ = map_server_path(candidate, mappings, resolved)
        else:
            target = candidate if os.path.isabs(candidate) else None
        if target and os.path.lexists(target):
            return target
    if mappings:
        target, _ = map_server_path(candidates[0], mappings, resolved)
        return target
    return None
