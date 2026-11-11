"""Lecture des noms de dossiers d'album : artiste, titre et année.

Les conventions courantes de Lidarr, beets ou d'un rangement manuel sont
reconnues ; un nom qui ne correspond à aucune n'est utilisé que comme titre.
Le résultat ne sert qu'à proposer des candidats : la comparaison des pistes
décide toujours du statut.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re

from .normalization import name_key

YEAR = r"(?P<year>(?:19|20)\d{2})"
SEP = r"\s+[-–—]\s+"
PATTERNS = [
    # 2014 - Artiste - Album
    re.compile(rf"^{YEAR}{SEP}(?P<artist>.+?){SEP}(?P<title>.+)$"),
    # Artiste - 2014 - Album
    re.compile(rf"^(?P<artist>.+?){SEP}{YEAR}{SEP}(?P<title>.+)$"),
    # Artiste - Album (2014)  /  Album (2014)  /  Album [2014]
    re.compile(rf"^(?P<title>.+?)\s*[(\[]{YEAR}[)\]]$"),
    # 2014 - Album
    re.compile(rf"^{YEAR}{SEP}(?P<title>.+)$"),
    # Album - 2014
    re.compile(rf"^(?P<title>.+?){SEP}{YEAR}$"),
]


@dataclass(frozen=True)
class FolderName:
    artist: str
    title: str
    year: str

    def key(self) -> tuple[str, str]:
        return name_key(self.artist), name_key(self.title)


def parse_folder(path: str | Path) -> FolderName:
    """Le dossier parent sert d'artiste quand le nom n'en contient pas."""
    path = Path(path)
    name = path.name.strip()
    parent = path.parent.name.strip()
    if re.fullmatch(r"(?:cd|disc|disk|disque)[\s._-]*\d+", name, flags=re.I):
        return parse_folder(path.parent)
    for pattern in PATTERNS:
        match = pattern.match(name)
        if not match:
            continue
        groups = match.groupdict()
        artist, title, year = groups.get("artist") or "", groups["title"].strip(), groups.get("year") or ""
        if not artist:
            # « Artiste - Album (2014) » : le préfixe n'est un artiste que s'il
            # correspond au dossier parent, sinon il fait partie du titre.
            parts = re.split(SEP, title, maxsplit=1)
            if len(parts) == 2 and name_key(parts[0]) == name_key(parent):
                artist, title = parts
        return FolderName(artist.strip() or parent, title.strip(), year)
    return FolderName(parent, name, "")
