"""Rapprochements explicables : une égalité de tags ne prouve pas celle des fichiers."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from itertools import combinations
import math
import os
from pathlib import Path
import re
import stat
from typing import Iterable
from uuid import UUID

from .models import Album, Inventory, Track
from .folder_names import parse_folder
from .normalization import name_key


DURATION_TOLERANCE = 2.0  # secondes : délais d'encodage MP3, sans prétendre à une empreinte audio
PHYSICAL = "Même fichier physique"
IDENTICAL = "Fichiers identiques octet pour octet"
PROBABLE = "Même édition probable"
MATCHING = "Pistes correspondantes, édition à vérifier"
DISTINCT = "Éditions distinctes"
REVIEW = "À vérifier"


@dataclass
class Comparison:
    status: str
    reasons: list[str] = field(default_factory=list)
    differences: list[str] = field(default_factory=list)
    track_pairs: list[tuple[Track, Track]] = field(default_factory=list)


def _ids(album: Album, key: str) -> set[str]:
    result = set()
    for value in album.values(key):
        try:
            result.add(str(UUID(value)))
        except (ValueError, AttributeError):
            pass
    return result


def _base_title(title: str) -> str:
    # Ces mentions élargissent uniquement la recherche des candidats. Elles
    # restent intactes dans les modèles et dans toutes les preuves affichées.
    title = re.sub(r"[\[(][^\])]*(?:deluxe|live|remaster|bonus|expanded|anniversary|special edition)[^\])]*[\])]",
                   "", title, flags=re.I)
    title = re.sub(r"\s*[-–:]\s*(?:deluxe|live|remaster|expanded|anniversary).*\Z", "", title, flags=re.I)
    return name_key(title)


def _signatures(album: Album, key: str) -> set[tuple[str, ...]]:
    return {tuple(sorted({name_key(value) for value in signature}))
            for signature in album.tag_signatures(key)}


def candidates(albums: Iterable[Album]) -> list[tuple[Album, Album]]:
    """Index par édition, groupe d'éditions et artiste/titre (pas par codec)."""
    albums = list(albums)
    index: dict[tuple, list[int]] = defaultdict(list)
    for i, album in enumerate(albums):
        for key in ("musicbrainz_albumid", "musicbrainz_releasegroupid"):
            for mbid in _ids(album, key):
                index[(key, mbid)].append(i)
        artists = _signatures(album, "albumartist")
        if not artists:
            artists = _signatures(album, "artist")
        titles = album.values("album")
        keys = set()
        if len(artists) == 1 and len(titles) == 1:
            artist, title = next(iter(artists)), _base_title(next(iter(titles)))
            if artist and title:
                keys.add(("name", artist, title))
        # Nom du dossier (« 2014 - Artiste - Album », « Album (2014) »…) : retrouve
        # les copies aux tags absents ou différents. Le statut reste décidé
        # par la comparaison des pistes.
        folder = parse_folder(album.path)
        artist, title = (name_key(folder.artist),), _base_title(folder.title)
        if artist[0] and title:
            keys.add(("name", artist, title))
        for key in keys:
            index[key].append(i)
    pairs = {pair for bucket in index.values() for pair in combinations(bucket, 2)}
    return [(albums[i], albums[j]) for i, j in sorted(pairs)]


def _physical_key(track: Track) -> tuple[int, int] | None:
    # Les fichiers peuvent avoir disparu ou avoir été remplacés depuis le scan,
    # notamment après restauration d'un inventaire local hors ligne.
    if track.device is None or track.inode is None or track.inode <= 0:
        return None
    try:
        path = Path(track.path)
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            return None
        current = os.stat(path, follow_symlinks=False)
        if (not stat.S_ISREG(current.st_mode)
                or (track.size, track.mtime_ns, track.device, track.inode)
                != (current.st_size, current.st_mtime_ns, current.st_dev, current.st_ino)):
            return None
        return current.st_dev, current.st_ino
    except OSError:
        return None


def _total(track: Track, key: str, number_key: str) -> int:
    value = track.number(key)
    if value:
        return value
    parts = track.value(number_key).split("/")
    try:
        return int(parts[1]) if len(parts) == 2 else 0
    except ValueError:
        return 0


def _known_missing(album: Album) -> bool:
    discs: dict[int, list[Track]] = defaultdict(list)
    for track in album.tracks:
        discs[track.disc].append(track)
    if not discs:
        return True
    if sorted(discs) != list(range(1, len(discs) + 1)):
        return True
    for tracks in discs.values():
        numbers = [t.number_in_disc for t in tracks]
        known = [n for n in numbers if n > 0]
        if known and sorted(known) != list(range(1, len(known) + 1)):
            return True
        totals = {_total(t, "tracktotal", "tracknumber") for t in tracks} - {0}
        if totals and (len(totals) != 1 or len(tracks) != next(iter(totals))):
            return True
    disc_totals = {_total(t, "disctotal", "discnumber") for t in album.tracks} - {0}
    return bool(disc_totals and (len(disc_totals) != 1 or len(discs) != next(iter(disc_totals))))


def _complete(album: Album) -> bool:
    return bool(album.complete and album.tracks and not _known_missing(album)
                and not any("regroupement ambigu" in issue.casefold() for issue in album.issues))


def _coherent(album: Album) -> bool:
    # Le nombre de pistes seul ne suffit pas pour conclure à une édition.
    if not _complete(album) or len(_signatures(album, "album")) != 1 or len(_signatures(album, "albumartist")) != 1:
        return False
    # La date d'origine peut différer entre les enregistrements d'une compilation.
    if any(len(album.values(key)) > 1 for key in ("date", "musicbrainz_releasegroupid")):
        return False
    return all(t.number_in_disc > 0 and t.value("title") and t.duration is not None
               and math.isfinite(t.duration) and t.duration > 0
               and t.value("album") and t.value("albumartist")
               and len(_recordings(t)) <= 1 for t in album.tracks)


def _release(album: Album) -> str | None:
    ids = _ids(album, "musicbrainz_albumid")
    if len(ids) != 1:
        return None
    release = next(iter(ids))
    # Un MBID renseigné sur une seule piste ne définit pas toute l'édition.
    return release if all(t.value("musicbrainz_albumid").lower() == release for t in album.tracks) else None


def _recordings(track: Track) -> set[str]:
    return {v.lower() for v in track.tags.get("musicbrainz_trackid", []) if v}


def _same_track(left: Track, right: Track) -> bool:
    if not left.value("title") or not right.value("title"):
        return False
    if name_key(left.value("title")) != name_key(right.value("title")):
        return False
    a, b = _recordings(left), _recordings(right)
    if len(a) > 1 or len(b) > 1 or (a and b and a != b):
        return False
    return (left.duration is not None and right.duration is not None
            and math.isfinite(left.duration) and math.isfinite(right.duration)
            and abs(left.duration - right.duration) <= DURATION_TOLERANCE)


def _differences(left: Album, right: Album) -> list[str]:
    result = []
    if left.value("album") != right.value("album"):
        result.append(f"Titres d'album : {left.value('album') or 'absent'} ↔ {right.value('album') or 'absent'}.")
    for key, label in (("albumartist", "Artistes d'album"), ("date", "Dates d'édition"),
                       ("originaldate", "Dates originales"), ("musicbrainz_albumid", "MBID d'édition")):
        if left.values(key) != right.values(key):
            result.append(f"{label} : {left.value(key) or 'absent'} ↔ {right.value(key) or 'absent'}.")
    if len(left.tracks) != len(right.tracks):
        result.append(f"Nombre de pistes : {len(left.tracks)} ↔ {len(right.tracks)}.")
    ld = len({t.disc for t in left.tracks})
    rd = len({t.disc for t in right.tracks})
    if ld != rd:
        result.append(f"Nombre de disques : {ld} ↔ {rd}.")
    a, b = left.ordered_tracks, right.ordered_tracks
    a_titles = Counter(name_key(t.value("title")) for t in a if t.value("title"))
    b_titles = Counter(name_key(t.value("title")) for t in b if t.value("title"))
    for label, tracks, missing in (("Gauche uniquement / bonus possible", a, a_titles - b_titles),
                                    ("Droite uniquement / bonus possible", b, b_titles - a_titles)):
        remaining = missing.copy()
        for track in tracks:
            key = name_key(track.value("title"))
            if remaining[key]:
                result.append(f"{label} : disque {track.disc}, piste {track.number_in_disc}, {track.value('title')}.")
                remaining[key] -= 1
    for i, (first, second) in enumerate(zip(a, b), 1):
        if (first.disc, first.number_in_disc) != (second.disc, second.number_in_disc):
            result.append(f"Position {i} : {first.disc}.{first.number_in_disc} ↔ {second.disc}.{second.number_in_disc}.")
        if name_key(first.value("title")) != name_key(second.value("title")):
            result.append(f"Ordre/titre à la position {i} : {first.value('title') or 'absent'} ↔ {second.value('title') or 'absent'}.")
        if first.duration is not None and second.duration is not None:
            delta = abs(first.duration - second.duration)
            if delta > DURATION_TOLERANCE:
                result.append(f"Durée à la position {i} : {first.duration:.1f} s ↔ {second.duration:.1f} s (écart {delta:.1f} s).")
        if _recordings(first) and _recordings(second) and _recordings(first) != _recordings(second):
            result.append(f"MBID d'enregistrement différents à la position {i}.")
    if {t.codec for t in a} != {t.codec for t in b}:
        result.append(f"Codecs : {', '.join(sorted({t.codec for t in a}))} ↔ {', '.join(sorted({t.codec for t in b}))}.")
    return result


def _cancelled(cancel) -> None:
    if cancel is not None and cancel.is_set():
        from .scanner import ScanCancelled
        raise ScanCancelled("Comparaison annulée.")


def compare_albums(left: Album, right: Album, cache=None, exact: bool = False, cancel=None) -> Comparison:
    """Comparer des dossiers ; SHA-256 uniquement sur demande explicite.

    Les identités physiques viennent du scanner, qui les laisse indisponibles
    sur les partages où leur fiabilité n'est pas établie.
    """
    _cancelled(cancel)
    a, b = left.ordered_tracks, right.ordered_tracks
    result = Comparison(REVIEW, differences=_differences(left, right), track_pairs=list(zip(a, b)))
    full = _complete(left) and _complete(right)
    identities_a = [_physical_key(t) for t in a]
    identities_b = [_physical_key(t) for t in b]
    if None in identities_a or None in identities_b or not a or not b:
        result.reasons.append("Identité indisponible pour au moins une piste (non fournie, fichier inaccessible ou modifié depuis le scan) ; aucune conclusion physique pour l'album.")
    elif full and Counter(identities_a) == Counter(identities_b):
        result.status = PHYSICAL
        result.reasons.append("Inventaires complets : chaque piste correspond à la même identité de fichier sur le même volume.")
        return result
    if not full:
        result.reasons.append("Inventaire incomplet, vide, regroupement ambigu ou numérotation indiquant des pistes manquantes/répétées : conclusion définitive impossible.")
        return result
    if exact:
        if cache is None:
            raise ValueError("Un cache est nécessaire au calcul SHA-256.")
        if Counter(t.size for t in a) == Counter(t.size for t in b):
            hashes_a, hashes_b = defaultdict(list), defaultdict(list)
            for tracks, hashes in ((a, hashes_a), (b, hashes_b)):
                for track in tracks:
                    _cancelled(cancel)
                    hashes[cache.hash_track(track, cancel=cancel)].append(track)
            if {k: len(v) for k, v in hashes_a.items()} == {k: len(v) for k, v in hashes_b.items()}:
                result.status = IDENTICAL
                result.track_pairs = [(x, y) for key in sorted(hashes_a)
                                      for x, y in zip(hashes_a[key], hashes_b[key])]
                result.reasons.append("SHA-256 en flux : correspondance une à une de tous les fichiers des deux inventaires complets, quels que soient leurs noms.")
                return result
            result.reasons.append("Les SHA-256 ne correspondent pas pour tous les fichiers.")
        else:
            result.reasons.append("Les tailles des fichiers excluent une égalité octet pour octet de l'album.")
    coherent = _coherent(left) and _coherent(right)
    matched = (coherent and len(a) == len(b)
               and all((x.disc, x.number_in_disc) == (y.disc, y.number_in_disc) and _same_track(x, y)
                       for x, y in zip(a, b)))
    edition_a, edition_b = _release(left), _release(right)
    if len(_ids(left, "musicbrainz_albumid")) > 1 or len(_ids(right, "musicbrainz_albumid")) > 1:
        result.reasons.append("Plusieurs MBID d'édition dans un même dossier : tags contradictoires.")
        return result
    if edition_a and edition_a == edition_b and any(
            _signatures(left, key) != _signatures(right, key)
            for key in ("albumartist", "album")):
        result.reasons.append("MBID d'édition commun mais titres/artistes d'album contradictoires.")
        return result
    if coherent and edition_a and edition_b and edition_a != edition_b:
        result.status = DISTINCT
        result.reasons.append("MBID d'édition valides et distincts, renseignés sur toutes les pistes ; chaque liste locale est cohérente.")
        result.reasons.append("Il s'agit d'un indice fondé sur les tags ; vérifier avec MusicBrainz pour confirmer les éditions.")
    elif matched and edition_a and edition_a == edition_b:
        result.status = PROBABLE
        result.reasons.append(f"Même MBID d'édition, mêmes disques/positions, titres et durées (tolérance {DURATION_TOLERANCE:g} s), sans contradiction des enregistrements renseignés.")
    elif matched:
        result.status = MATCHING
        result.reasons.append(f"Titres, positions et durées correspondent (tolérance {DURATION_TOLERANCE:g} s). Les tags ne prouvent ni l'identité audio ni celle de l'édition.")
    else:
        result.reasons.append("Liste des pistes, durées, numérotation ou tags insuffisants/contradictoires.")
        if edition_a and edition_a == edition_b:
            result.reasons.append("Le MBID d'édition commun contredit les listes locales : aucune édition déduite automatiquement.")
    return result


def exact_file_groups(inventory: Inventory, cache, cancel=None, progress=None) -> list[list[Track]]:
    """Recherche exacte indépendante des noms, préfiltrée par taille."""
    sizes: dict[int, list[Track]] = defaultdict(list)
    seen = set()
    for album in inventory.albums:
        for track in album.tracks:
            if track.path not in seen:
                sizes[track.size].append(track)
                seen.add(track.path)
    tracks = [t for group in sizes.values() if len(group) > 1 for t in group]
    hashes: dict[tuple[int, str], list[Track]] = defaultdict(list)
    for i, track in enumerate(tracks, 1):
        _cancelled(cancel)
        digest = cache.hash_track(track, cancel=cancel)
        hashes[(track.size, digest)].append(track)
        if progress:
            progress(i, len(tracks), track.path)
    _cancelled(cancel)
    return [group for group in hashes.values() if len(group) > 1]
