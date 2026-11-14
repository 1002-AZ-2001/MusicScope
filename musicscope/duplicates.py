"""Groupes d'albums en double et copie recommandée à conserver.

Un groupe réunit des albums reliés par une comparaison concluante (copie
exacte, même édition probable, pistes correspondantes). Une paire « À vérifier »
n'est ajoutée que si les noms de dossiers désignent le même album et la même
année ; le groupe est alors marqué « À vérifier » et rien n'est présélectionné.
Les éditions distinctes ne sont jamais regroupées.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os

from .audit import audit_album
from .comparison import DISTINCT, IDENTICAL, MATCHING, PHYSICAL, PROBABLE, REVIEW, Comparison, _known_missing
from .folder_names import parse_folder
from .models import Album

EXACT = "Copie exacte"
LIKELY = "Même album probable"
UNSURE = "À vérifier"
CONFIDENCE_ORDER = {EXACT: 0, LIKELY: 1, UNSURE: 2}
CODEC_RANK = {"FLAC": 3, "ALAC": 3, "Opus": 2, "Ogg Vorbis": 2, "AAC": 2, "MP3": 1}
TAG_FIELDS = ("albumartist", "album", "title", "tracknumber", "date")


@dataclass
class CopyScore:
    album: Album
    score: tuple
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    linked_files: int = 0


@dataclass
class DuplicateGroup:
    albums: list[Album]
    confidence: str
    scores: list[CopyScore]
    keep: int | None
    reasons: list[str] = field(default_factory=list)
    shared_files: bool = False

    @property
    def title(self) -> str:
        album = self.albums[self.keep if self.keep is not None else 0]
        folder = parse_folder(album.path)
        return album.value("album") or folder.title

    @property
    def extra_bytes(self) -> int:
        if self.keep is None:
            return 0
        return sum(track.size for index, album in enumerate(self.albums) if index != self.keep
                   for track in album.tracks)

    def to_remove(self) -> list[Album]:
        return [album for index, album in enumerate(self.albums) if index != self.keep] if self.keep is not None else []


def physical_files(album: Album) -> list[tuple[int, int, int]]:
    """(périphérique, inode, liens) de chaque piste, lus directement : sur un partage réseau,
    l'inventaire n'enregistre pas l'identité physique, mais le serveur peut la transmettre."""
    result = []
    for track in album.tracks:
        try:
            info = os.stat(track.path, follow_symlinks=False)
        except OSError:
            continue
        if (info.st_size, info.st_mtime_ns) == (track.size, track.mtime_ns) and info.st_ino:
            result.append((info.st_dev, info.st_ino, info.st_nlink))
    return result


def score_album(album: Album) -> CopyScore:
    """Plus le tuple est grand, meilleure est la copie. Les raisons sont lisibles."""
    result = CopyScore(album, ())
    complete = album.complete and bool(album.tracks) and not _known_missing(album)
    (result.strengths if complete else result.weaknesses).append(
        "album complet" if complete else "pistes manquantes ou illisibles")
    codecs = {track.codec for track in album.tracks}
    quality = min((CODEC_RANK.get(codec, 0) for codec in codecs), default=0)
    label = ", ".join(sorted(codecs)) or "aucun codec"
    if quality == 3:
        result.strengths.append(f"sans perte ({label})")
    elif quality:
        result.weaknesses.append(f"avec perte ({label})")
    filled = sum(bool(track.value(key)) for track in album.tracks for key in TAG_FIELDS)
    ratio = filled / (len(album.tracks) * len(TAG_FIELDS)) if album.tracks else 0
    (result.strengths if ratio == 1 else result.weaknesses).append(f"tags renseignés à {ratio:.0%}")
    has_mbid = bool(album.values("musicbrainz_albumid"))
    if has_mbid:
        result.strengths.append("identifiant MusicBrainz présent")
    issues = len(set(album.issues + audit_album(album)))
    if issues:
        result.weaknesses.append(f"{issues} constat(s) d’audit")
    size = sum(track.size for track in album.tracks)
    linked = sum(links > 1 for _, _, links in physical_files(album))
    if linked:
        # À qualité égale, garder la copie liée : la retirer ne libérerait aucun espace.
        result.strengths.append(f"{linked} fichier(s) liés physiquement (Lidarr, qBittorrent)")
    result.linked_files = linked
    result.score = (complete, quality, len(album.tracks), bool(linked), round(ratio, 3), has_mbid, -issues, size)
    return result


def _confidence(status: str) -> str | None:
    if status in (PHYSICAL, IDENTICAL):
        return EXACT
    if status in (PROBABLE, MATCHING):
        return LIKELY
    if status == REVIEW:
        return UNSURE
    return None


def _same_folder_album(left: Album, right: Album) -> bool:
    a, b = parse_folder(left.path), parse_folder(right.path)
    return a.key() == b.key() and bool(a.year) and a.year == b.year


def duplicate_groups(pairs: list[tuple[Album, Album, Comparison]]) -> list[DuplicateGroup]:
    parent: dict[int, int] = {}
    albums: dict[int, Album] = {}

    def find(key: int) -> int:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    links: list[tuple[int, int, str]] = []
    for left, right, comparison in pairs:
        if comparison.status == DISTINCT or left is right:
            continue
        confidence = _confidence(comparison.status)
        if confidence is None or (confidence == UNSURE and not _same_folder_album(left, right)):
            continue
        for album in (left, right):
            albums.setdefault(id(album), album)
            parent.setdefault(id(album), id(album))
        links.append((id(left), id(right), confidence))
        parent[find(id(left))] = find(id(right))
    members: dict[int, list[int]] = {}
    for key in albums:
        members.setdefault(find(key), []).append(key)
    groups = []
    for keys in members.values():
        group_albums = sorted((albums[key] for key in keys), key=lambda album: album.path)
        inside = set(keys)
        confidences = [confidence for left, right, confidence in links if left in inside and right in inside]
        confidence = max(confidences, key=CONFIDENCE_ORDER.__getitem__)
        scores = [score_album(album) for album in group_albums]
        ranked = sorted(range(len(scores)), key=lambda index: scores[index].score, reverse=True)
        best, second = ranked[0], ranked[1]
        keep = best if scores[best].score[:-1] != scores[second].score[:-1] else None
        reasons = []
        if confidence == UNSURE:
            keep = None
            reasons.append("Correspondance incertaine : vérifiez les deux albums, puis choisissez vous-même la copie à garder.")
        elif keep is None:
            reasons.append("Copies de qualité équivalente : choisissez la copie à garder.")
        else:
            reasons.append("Copie recommandée : " + ", ".join(scores[best].strengths or ["meilleur score"]) + ".")
        years = {parse_folder(album.path).year for album in group_albums} - {""}
        if len(years) > 1:
            reasons.append("Années différentes dans les noms de dossiers : vérifier qu'il ne s'agit pas d'une réédition.")
        identities = [{(device, inode) for device, inode, _ in physical_files(album)} for album in group_albums]
        shared = any(ids & other for position, ids in enumerate(identities) for other in identities[position + 1:])
        if shared:
            keep = None
            reasons.insert(0, "Ces dossiers partagent les mêmes fichiers (liens physiques, par exemple Lidarr et "
                              "qBittorrent) : ce ne sont pas des copies. En retirer un ne libère rien et peut casser "
                              "le partage ou la bibliothèque.")
            reasons[:] = [reason for reason in reasons if not reason.startswith(("Copie recommandée", "Copies de qualité"))]
        removable = [scores[index] for index in range(len(scores)) if index != keep] if keep is not None else []
        if any(score.linked_files for score in removable):
            reasons.append("Une copie à retirer contient des fichiers liés ailleurs : la quarantaine ne libérera pas leur "
                           "espace et peut interrompre le partage qBittorrent.")
        groups.append(DuplicateGroup(group_albums, confidence, scores, keep, reasons, shared))
    groups.sort(key=lambda group: (CONFIDENCE_ORDER[group.confidence], group.title.casefold()))
    return groups
