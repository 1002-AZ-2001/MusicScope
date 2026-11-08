"""Diagnostic du catalogue Jellyfin par GET, sans modification du serveur.

Les correspondances de chemins sont explicites. Un catalogue tronqué ou un
chemin serveur non résolu ne devient jamais une preuve d'absence du catalogue.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from .cache import check_cancel
from .models import Inventory


@dataclass(frozen=True)
class PathMapping:
    server_prefix: str
    local_prefix: str


@dataclass
class JellyfinFinding:
    status: str
    server_path: str = ""
    local_path: str = ""
    item_id: str = ""
    title: str = ""
    reason: str = ""


@dataclass
class Diagnosis:
    findings: list[JellyfinFinding] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    complete: bool = False


class JellyfinUnavailable(Exception):
    """Transport ou catalogue incomplet ; le message ne contient aucun secret."""


def _server_parts(value: str) -> tuple[bool, tuple[str, ...], tuple[str, ...]] | None:
    if not isinstance(value, str) or not value or "\0" in value:
        return None
    windows = bool(re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith(("\\\\", "//")))
    path = PureWindowsPath(value) if windows else PurePosixPath(value)
    if not path.is_absolute() or ".." in path.parts:
        return None
    parts = path.parts
    normalized = tuple(part.casefold() for part in parts) if windows else parts
    return windows, normalized, parts


def map_server_path(server_path: str, mappings: list[PathMapping]) -> tuple[str | None, str]:
    """Appliquer le préfixe le plus précis, en respectant les limites de dossier.

    Deux destinations différentes pour le même préfixe restent ambiguës. Les
    segments ``..`` et les sorties de la racine locale sont refusés.
    """
    source = _server_parts(server_path)
    if source is None:
        return None, "Chemin serveur absent, relatif ou contenant une remontée de dossier."
    windows, parts, original = source
    matches = []
    for mapping in mappings:
        prefix = _server_parts(mapping.server_prefix)
        if prefix is None or prefix[0] != windows:
            continue
        normalized = prefix[1]
        if parts[:len(normalized)] != normalized:
            continue
        local = Path(mapping.local_prefix).expanduser()
        if not local.is_absolute():
            return None, "La destination d'une correspondance doit être un chemin local absolu."
        suffix = original[len(normalized):]
        target = local.joinpath(*suffix)
        try:
            if not target.resolve().is_relative_to(local.resolve()):
                return None, "La correspondance sortirait du dossier local configuré."
        except (OSError, RuntimeError, ValueError):
            return None, "La destination locale ne peut pas être résolue."
        matches.append((len(normalized), os.path.normcase(os.path.abspath(target)), str(target)))
    if not matches:
        return None, "Aucune correspondance explicite pour ce préfixe serveur."
    longest = max(match[0] for match in matches)
    best = [match for match in matches if match[0] == longest]
    if len({match[1] for match in best}) != 1:
        return None, "Plusieurs correspondances de même priorité proposent des destinations différentes."
    return best[0][2], "Correspondance explicite par le préfixe le plus précis."


def _path_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


class _SameOriginRedirect(HTTPRedirectHandler):
    """Ne pas transmettre X-Emby-Token à un autre hôte ou protocole."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if _origin(newurl) != _origin(req.full_url):
            raise JellyfinUnavailable("Redirection Jellyfin vers une autre origine refusée.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class JellyfinClient:
    def __init__(self, server_url: str, api_key: str, opener=urlopen,
                 page_size: int = 500, max_pages: int = 1000):
        parsed = urlsplit(server_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment):
            raise ValueError("Adresse Jellyfin HTTP(S) sans identifiants, paramètres ni fragment requise.")
        try:
            parsed.port
        except ValueError as exc:
            raise ValueError("Port Jellyfin invalide.") from exc
        if not api_key.strip() or any(character in api_key for character in "\r\n"):
            raise ValueError("Une clé API Jellyfin valide est requise.")
        if not 1 <= page_size <= 1000 or not 1 <= max_pages <= 1000:
            raise ValueError("Pagination Jellyfin hors limites.")
        self.server_url = server_url.rstrip("/") + "/"
        self._api_key = api_key.strip()
        self.opener = build_opener(_SameOriginRedirect()).open if opener is urlopen else opener
        self.page_size, self.max_pages = page_size, max_pages

    def _page(self, start: int) -> dict:
        query = urlencode({"IncludeItemTypes": "Audio", "Recursive": "true", "Fields": "Path",
                           "StartIndex": start, "Limit": self.page_size, "EnableTotalRecordCount": "true"})
        request = Request(urljoin(self.server_url, "Items") + "?" + query,
                          headers={"X-Emby-Token": self._api_key, "Accept": "application/json",
                                   "User-Agent": "MusicScope/0.2.0 (https://github.com/1002-AZ-2001/MusicScope)"},
                          method="GET")
        try:
            with self.opener(request, timeout=20) as response:
                body = response.read(16 * 1024 * 1024 + 1)
            if len(body) > 16 * 1024 * 1024:
                raise JellyfinUnavailable("Réponse Jellyfin trop volumineuse.")
            page = json.loads(body)
            if not isinstance(page, dict) or not isinstance(page.get("Items"), list):
                raise JellyfinUnavailable("Catalogue Jellyfin illisible ou incomplet.")
            items = page["Items"]
            if len(items) > self.page_size or any(not isinstance(item, dict) for item in items):
                raise JellyfinUnavailable("Page Jellyfin invalide.")
            total = page.get("TotalRecordCount")
            if total is not None and (type(total) is not int or total < 0):
                raise JellyfinUnavailable("Nombre total d'éléments Jellyfin invalide.")
            for item in items:
                if not all(isinstance(item.get(field, ""), str) for field in ("Id", "Path", "Name")):
                    raise JellyfinUnavailable("Entrée du catalogue Jellyfin invalide.")
            return page
        except HTTPError as exc:
            raise JellyfinUnavailable(f"Jellyfin indisponible (HTTP {exc.code}).") from exc
        except (URLError, OSError, TimeoutError) as exc:
            raise JellyfinUnavailable("Connexion Jellyfin indisponible ou délai dépassé.") from exc
        except (ValueError, UnicodeError) as exc:
            raise JellyfinUnavailable("Réponse Jellyfin illisible.") from exc

    def diagnose(self, inventory: Inventory, mappings: list[PathMapping], cancel=None, progress=None) -> Diagnosis:
        result = Diagnosis()
        items = []
        seen_ids = set()
        expected_total = None
        start = 0
        try:
            for _ in range(self.max_pages):
                check_cancel(cancel)
                page = self._page(start)
                page_items = page["Items"]
                total = page.get("TotalRecordCount")
                if expected_total is not None and total is not None and expected_total != total:
                    raise JellyfinUnavailable("Le catalogue Jellyfin a changé pendant la pagination ; relancer le diagnostic.")
                if total is not None:
                    expected_total = total
                for item in page_items:
                    if not item.get("Id") or item["Id"] in seen_ids:
                        raise JellyfinUnavailable("Pagination Jellyfin répétée ou identifiant d'élément absent.")
                    seen_ids.add(item["Id"])
                items.extend(page_items)
                start += len(page_items)
                if progress:
                    progress(start, expected_total or 0, "Catalogue Jellyfin")
                if expected_total is not None:
                    if start > expected_total:
                        raise JellyfinUnavailable("Le nombre d'éléments Jellyfin contredit le total annoncé.")
                    if start == expected_total:
                        result.complete = True
                        break
                    if not page_items:
                        raise JellyfinUnavailable("Catalogue Jellyfin tronqué avant le total annoncé.")
                elif len(page_items) < self.page_size:
                    result.complete = True
                    break
            else:
                raise JellyfinUnavailable("Limite de pagination Jellyfin atteinte ; diagnostic incomplet.")
        except JellyfinUnavailable as exc:
            result.reasons.append(str(exc))
            result.reasons.append("Catalogue incomplet : aucune absence du catalogue ne peut être conclue.")
        check_cancel(cancel)
        self._compare(inventory, mappings, items, result, cancel, progress)
        result.reasons.append(f"{len(items)} entrées de catalogue examinées ; {sum(len(a.tracks) for a in inventory.albums)} fichiers inventoriés.")
        return result

    @staticmethod
    def _compare(inventory, mappings, items, result, cancel, progress):
        local = {_path_key(track.path): track.path for album in inventory.albums for track in album.tracks}
        try:
            root_available = stat.S_ISDIR(Path(inventory.root).stat().st_mode)
        except OSError:
            root_available = False
        if not root_available:
            result.reasons.append("Racine locale inaccessible : aucune disparition de fichier ne peut être affirmée.")
        represented = set()
        server_keys = set()
        mapped_keys = set()
        unresolved = False
        for index, item in enumerate(items, 1):
            check_cancel(cancel)
            server = item.get("Path", "")
            target, reason = map_server_path(server, mappings)
            finding = JellyfinFinding("", server, target or "", item.get("Id", ""), item.get("Name", ""), reason)
            parsed = _server_parts(server)
            server_key = (parsed[0], parsed[1]) if parsed else None
            if target is None:
                finding.status = "Chemin non résolu"
                unresolved = True
            else:
                key = _path_key(target)
                if key in local:
                    represented.add(key)
                if not root_available:
                    finding.status = "Accès local indéterminé"
                    finding.reason = "La racine locale est inaccessible ; vérifier le montage avant de conclure à une disparition."
                else:
                    try:
                        path = Path(target)
                        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                            finding.status = "Lien local ignoré"
                            finding.reason = "Le catalogue pointe vers un lien exclu du parcours local."
                        elif stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                            finding.status = "Correspondance" if key in local else "Fichier hors inventaire"
                            finding.reason = ("Chemin du catalogue associé à un fichier présent et connu de l'inventaire local."
                                              if key in local else "Le fichier existe mais n'est pas dans cet inventaire ; vérifier la racine, le format et la date du scan.")
                        else:
                            finding.status = "Fichier local absent"
                            finding.reason = "Le chemin local désigne un objet qui n'est pas un fichier audio inventorié."
                    except FileNotFoundError:
                        finding.status = "Fichier local absent"
                        finding.reason = "Le chemin traduit du catalogue n'existe pas sur ce poste ; vérifier également le montage et la correspondance."
                    except OSError:
                        finding.status = "Accès local indéterminé"
                        finding.reason = "Le chemin local ne peut pas être consulté ; vérifier le partage et les droits."
                if (server_key is not None and server_key in server_keys) or key in mapped_keys:
                    finding.status = "Entrée dupliquée"
                    finding.reason = "Plusieurs entrées du catalogue correspondent au même chemin serveur ou local."
                mapped_keys.add(key)
            if server_key is not None:
                server_keys.add(server_key)
            result.findings.append(finding)
            if progress:
                progress(index, len(items), server)
        for key, path in sorted(local.items()):
            if key in represented:
                continue
            known_absent = result.complete and not unresolved
            result.findings.append(JellyfinFinding(
                "Absent du catalogue" if known_absent else "Présence au catalogue indéterminée",
                local_path=path,
                reason="Aucune entrée du catalogue complet ne correspond à ce fichier inventorié." if known_absent else
                       "Le catalogue est incomplet ou contient des chemins non résolus ; une absence ne peut pas être affirmée."))
        if unresolved:
            result.reasons.append("Des chemins serveur restent non résolus ; la présence des fichiers non rapprochés reste indéterminée.")
