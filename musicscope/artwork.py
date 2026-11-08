"""Pochettes par édition, consultées à la demande et conservées localement."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, build_opener, urlopen

from .musicbrainz import ServiceUnavailable, UUID
from .recognition import _read_url


@dataclass(frozen=True)
class Artwork:
    id: str
    source_url: str
    image_path: str
    front: bool
    release_id: str
    checksum: str


def _safe_url(url: str) -> str:
    if not isinstance(url, str):
        raise ServiceUnavailable("Adresse de pochette invalide.")
    try:
        parts = urlsplit(url)
        host, port = parts.hostname or "", parts.port
    except ValueError as exc:
        raise ServiceUnavailable("Adresse de pochette invalide.") from exc
    allowed = any(host == domain or host.endswith("." + domain) for domain in ("coverartarchive.org", "archive.org"))
    if (not allowed or parts.scheme not in {"https", "http"} or parts.username or parts.password
            or port not in {None, 80, 443}):
        raise ServiceUnavailable("Domaine de pochette non autorisé.")
    # Les réponses historiques CAA exposent encore des URLs HTTP Archive.org.
    return urlunsplit(("https", host, parts.path, parts.query, ""))


class _ArtworkRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, _safe_url(newurl))


class ArtworkClient:
    """Télécharge les faces avant (4 maximum), ou la première image disponible.

    Chaque image est bornée à 10 Mio, servie depuis CAA/Archive.org en HTTPS,
    et doit posséder une signature JPEG ou PNG. Une empreinte égale est une
    preuve sur le fichier image téléchargé, jamais sur l'édition musicale.
    """
    def __init__(self, cache_dir: str | Path, opener=urlopen):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.opener = build_opener(_ArtworkRedirect()).open if opener is urlopen else opener

    def close(self):
        pass

    def fetch(self, release_id: str) -> list[Artwork]:
        if not isinstance(release_id, str) or not UUID.fullmatch(release_id):
            raise ValueError("Un MBID d'édition valide est nécessaire pour les pochettes.")
        release_id = release_id.lower()
        directory = self.cache_dir / release_id
        directory.mkdir(exist_ok=True)
        manifest = directory / "manifest.json"
        if manifest.is_file():
            try:
                saved = json.loads(manifest.read_text("utf-8"))
                if 0 <= time.time() - saved["fetched"] < 7 * 86400:
                    result = []
                    for entry in saved["images"]:
                        item = Artwork(**entry)
                        image = Path(item.image_path)
                        if (image.parent.resolve() != directory.resolve()
                                or item.release_id != release_id or _safe_url(item.source_url) != item.source_url
                                or hashlib.sha256(image.read_bytes()).hexdigest() != item.checksum):
                            raise ValueError("Cache modifié")
                        result.append(item)
                    return result
            except (OSError, ValueError, KeyError, TypeError, ServiceUnavailable):
                pass
        raw = _read_url(f"https://coverartarchive.org/release/{release_id}", self.opener)
        if raw is None:
            return []
        try:
            metadata = json.loads(raw)
            images = metadata["images"]
            if not isinstance(images, list) or any(not isinstance(item, dict) for item in images):
                raise ValueError("Images invalides")
            front = [item for item in images if item.get("front") is True]
            selected = (front if front else images[:1])[:4]
            result = []
            for index, entry in enumerate(selected):
                thumbnails = entry.get("thumbnails", {})
                if not isinstance(thumbnails, dict):
                    raise ValueError("Miniatures invalides")
                source = _safe_url(thumbnails.get("500") or thumbnails.get("large") or entry["image"])
                data = _read_url(source, self.opener, max_bytes=10 * 1024 * 1024)
                if data is None:
                    raise ServiceUnavailable("Image de pochette indisponible malgré son entrée dans le catalogue.")
                if data.startswith(b"\x89PNG\r\n\x1a\n"):
                    suffix = ".png"
                elif data.startswith(b"\xff\xd8\xff"):
                    suffix = ".jpg"
                else:
                    raise ServiceUnavailable("La pochette reçue n'est pas une image PNG ou JPEG.")
                checksum = hashlib.sha256(data).hexdigest()
                destination = directory / (checksum + suffix)
                temporary = destination.with_suffix(suffix + ".tmp")
                temporary.write_bytes(data)
                temporary.replace(destination)
                result.append(Artwork(str(entry.get("id", index)), source, str(destination),
                                      entry.get("front") is True, release_id, checksum))
        except (ValueError, TypeError, KeyError) as exc:
            raise ServiceUnavailable("Réponse Cover Art Archive illisible.") from exc
        from dataclasses import asdict
        temporary = manifest.with_suffix(".tmp")
        temporary.write_text(json.dumps({"fetched": time.time(), "images": [asdict(item) for item in result]}), "utf-8")
        temporary.replace(manifest)
        return result


def compare_artworks(left: Artwork, right: Artwork) -> str:
    if left.checksum == right.checksum:
        return "Images téléchargées identiques (SHA-256) ; cela ne prouve pas l'identité des éditions."
    return "Images téléchargées différentes ; une taille ou compression différente peut expliquer cet écart."
