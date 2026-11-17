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
    # (2014) Album  /  [2014] Album
    re.compile(rf"^[(\[]{YEAR}[)\]]\s*(?:[-–—]\s*)?(?P<title>.+)$"),
]
# Mentions techniques des téléchargements, retirées en fin de nom : « [FLAC] », « (24-96) »,
# « - WEB », « [320] »… Elles masquaient l'année et le titre.
_TECH = (r"(?:flac|mp3|aac|alac|ogg|opus|wav|ape|dsd|web|web-?dl|cd|cdrip|vinyl|lp|hi-?res|lossless|"
         r"v0|v2|\d{3}\s*k(?:bps)?|320|256|192|128|\d{2}\s*[-_/]\s*\d{2,3}(?:[.,]\d)?(?:\s*khz)?|\d{2}\s*bits?"
         r"(?:\s*[-_/ ]?\s*\d{2,3}(?:[.,]\d)?\s*khz)?)")
TECH_SUFFIX = re.compile(rf"(?:\s*[\[(]\s*{_TECH}(?:\s*[-_/ ,]\s*{_TECH})*\s*[\])]|\s+[-–—]\s+{_TECH})\s*$", re.I)
DISC = re.compile(r"(?:cd|disc|disk|disque)[\s._-]*\d+(?:\s*[-–—:]\s*.*)?", re.I)


def _clean(name: str) -> str:
    previous = None
    while previous != name:
        previous, name = name, TECH_SUFFIX.sub("", name).strip()
    return name or previous


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
    name = _clean(path.name.strip())
    parent = path.parent.name.strip()
    if DISC.fullmatch(name):  # « CD1 », « Disc 1 - Bonus »
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
