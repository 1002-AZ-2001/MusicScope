"""Lidarr : demander une relecture des dossiers modifiés par MusicScope.

Sources (dépôt Lidarr, branche develop) : authentification par l'en-tête ``X-Api-Key``
(Lidarr.Http/Authentication/AuthenticationBuilderExtensions.cs) ; commande
``RescanFolders`` (NzbDrone.Core/MediaFiles/Commands/RescanFoldersCommand.cs : Folders,
Filter, AddNewArtists, ArtistIds), nom dérivé de la classe sans « Command »
(Messaging/Commands/Command.cs) ; version par ``GET api/v1/system/status``.

MusicScope n'envoie que cette commande, avec ``addNewArtists`` à faux : Lidarr relit les
dossiers indiqués et constate les fichiers déplacés, sans jamais ajouter d'artiste.
"""
from __future__ import annotations

from http.client import HTTPException
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen


class LidarrUnavailable(RuntimeError):
    pass


class _SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        old, new = urlsplit(req.full_url), urlsplit(newurl)
        if (old.scheme, old.hostname, old.port) != (new.scheme, new.hostname, new.port):
            raise LidarrUnavailable("Redirection Lidarr vers une autre origine refusée.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class LidarrClient:
    def __init__(self, server_url: str, api_key: str, opener=urlopen):
        parsed = urlsplit(server_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError("Adresse Lidarr HTTP(S) sans identifiants, paramètres ni fragment requise.")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("Port Lidarr invalide.") from exc
        if not re.fullmatch(r"[\x21-\x7e]{8,128}", api_key.strip()):
            raise ValueError("Clé API Lidarr invalide (Paramètres → Général → Sécurité) : recopiez-la.")
        self.server_url = server_url.rstrip("/") + "/"
        self._api_key = api_key.strip()
        self.opener = build_opener(_SameOriginRedirect()).open if opener is urlopen else opener
        self.timeout = 20

    def _request(self, path: str, body: dict | None = None):
        headers = {"Accept": "application/json", "X-Api-Key": self._api_key, "User-Agent": "MusicScope"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = Request(urljoin(self.server_url, path), data=data, headers=headers,
                          method="POST" if body is not None else "GET")
        try:
            with self.opener(request, timeout=self.timeout) as response:
                content = response.read(4 * 1024 * 1024 + 1)
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise LidarrUnavailable(f"Lidarr refuse la clé API (HTTP {exc.code}).") from exc
            raise LidarrUnavailable(f"Lidarr indisponible (HTTP {exc.code}).") from exc
        except (URLError, OSError, TimeoutError, HTTPException) as exc:
            raise LidarrUnavailable("Connexion Lidarr impossible : vérifiez l’adresse, le port et le réseau.") from exc
        try:
            return json.loads(content) if content.strip() else None
        except (ValueError, UnicodeError) as exc:
            raise LidarrUnavailable("Réponse Lidarr illisible : l’adresse désigne-t-elle bien Lidarr ?") from exc

    def version(self) -> str:
        status = self._request("api/v1/system/status")
        if not isinstance(status, dict):
            raise LidarrUnavailable("Statut Lidarr illisible.")
        value = next((status[key] for key in status if key.casefold() == "version"), "")
        return str(value)[:40]

    def rescan(self, server_folders: list[str]) -> None:
        """Relecture des dossiers (chemins vus par Lidarr), sans ajouter d'artiste."""
        if not server_folders:
            return
        self._request("api/v1/command", {"name": "RescanFolders", "folders": sorted(set(server_folders)),
                                         "addNewArtists": False})
