"""Recherche de fichiers en double avec critères combinables, puis quarantaine.

Les critères cochés doivent tous concorder (comme une recherche « nom ET
taille »). Ils sont appliqués du moins coûteux au plus coûteux : clés simples
(nom, taille, date, artiste/titre), durée, contenu SHA-256, puis empreinte audio.

Les fichiers retirés vont dans `<racine>/_MusicScope_doublons/<chemin relatif>`
par renommage sans remplacement sur le même volume, avec un journal JSON
restaurable. La seule suppression définitive est « vider la quarantaine ».
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat as stat_module
import unicodedata
from threading import Event
from typing import Callable
from uuid import uuid4

from .cache import Cache, check_cancel
from .metadata import AUDIO_EXTENSIONS, read_track
from .models import Track
from .normalization import name_key
from .organizer import (
    QUARANTINE_DIR, OrganizeError, _check_ancestors, _collision, _fsync_directory, _mkdir_safe, _remove_reservation,
    _rename_without_replace, _save_journal,
)

CONTENT = "Contenu identique (SHA-256)"
SONG = "Même morceau (artiste, titre, durée)"
NAME = "Même nom"
SIZE = "Même taille"
DATE = "Même date de modification"
FINGERPRINT = "Empreinte audio (Chromaprint)"
CRITERIA = (CONTENT, SONG, NAME, SIZE, DATE, FINGERPRINT)
LOSSLESS = {"FLAC", "ALAC"}
CODEC_RANK = {"FLAC": 3, "ALAC": 3, "Opus": 2, "Ogg Vorbis": 2, "AAC": 2, "MP3": 1}
FILE_JOURNAL_VERSION = 2

Progress = Callable[[int, int, str], None] | None


@dataclass
class ScanOptions:
    criteria: frozenset[str]
    min_size: int = 1
    max_size: int | None = None
    include_extensions: frozenset[str] = frozenset()
    exclude_extensions: frozenset[str] = frozenset()
    exclude_folders: tuple[str, ...] = ()
    skip_hidden: bool = True
    duration_tolerance: float = 2.0
    fingerprint_threshold: float = 0.85

    def __post_init__(self):
        unknown = set(self.criteria) - set(CRITERIA)
        if unknown or not self.criteria:
            raise ValueError("Choisissez au moins un critère valide.")


@dataclass
class FileRecord:
    path: str
    size: int
    mtime_ns: int
    device: int
    inode: int
    track: Track | None = None
    nlink: int = 1
    aliases: list[str] = field(default_factory=list)
    seeded: str = ""  # torrent qBittorrent qui utilise ce fichier : jamais coché ni déplacé
    spectrum: str = ""  # verdict d'authenticité du sans-perte (« Suspect », « À vérifier »…)

    @property
    def linked(self) -> bool:
        """Fichier présent ailleurs par lien physique (Lidarr, qBittorrent) : le retirer ne libère rien."""
        return self.nlink > 1

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)

    @property
    def extension(self) -> str:
        return os.path.splitext(self.path)[1].casefold()

    @property
    def codec(self) -> str:
        return self.track.codec if self.track else self.extension.lstrip(".").upper()

    @property
    def duration(self) -> float | None:
        return self.track.duration if self.track else None

    @property
    def bitrate(self) -> int | None:
        if self.duration:
            return round(self.size * 8 / self.duration / 1000)
        return None


@dataclass
class FileGroup:
    files: list[FileRecord]
    criteria: tuple[str, ...]

    @property
    def shared_inodes(self) -> bool:
        return any(record.aliases for record in self.files)

    @property
    def recoverable_bytes(self) -> int:
        """Espace libéré en gardant la meilleure copie ; un fichier lié ailleurs ne libère rien."""
        best = self.best()
        return sum(record.size for record in self.files if record is not best and not record.linked)

    def best(self) -> FileRecord:
        # Mémorisé : appelé pour chaque fichier par les aides de sélection et l'affichage.
        key = tuple(id(record) for record in self.files)
        cached = self.__dict__.get("_best")
        if cached is None or cached[0] != key:
            cached = (key, max(self.files, key=quality_key))
            self.__dict__["_best"] = cached
        return cached[1]


def quality_key(record: FileRecord) -> tuple:
    """Meilleure copie : sans perte, débit, puis copie liée (la retirer ne libérerait rien),
    tags complets, chemin le moins profond."""
    track = record.track
    tags = sum(bool(track.value(key)) for key in ("artist", "title", "album", "tracknumber")) if track else 0
    rank = CODEC_RANK.get(record.codec, 0)
    if rank == 3 and record.spectrum in ("Suspect", "À vérifier"):
        rank = 0.5 if record.spectrum == "Suspect" else 2  # faux FLAC probable : sous un vrai MP3
    return (rank, record.bitrate or 0, record.linked, tags, -record.path.count(os.sep),
            -len(record.path), record.path)


def _excluded(path: str, folders: tuple[str, ...]) -> bool:
    return any(path == folder or path.startswith(folder.rstrip(os.sep) + os.sep) for folder in folders)


def collect_files(root: str | Path, options: ScanOptions, tracks: dict[str, Track] | None = None,
                  cancel: Event | None = None, progress: Progress = None) -> list[FileRecord]:
    """Tous les fichiers réguliers sous la racine, filtrés ; liens et quarantaine ignorés."""
    root = Path(root).absolute()
    tracks = tracks or {}
    folders = tuple(str(Path(folder) if Path(folder).is_absolute() else root / folder) for folder in options.exclude_folders)
    records, pending = [], [root]
    while pending:
        check_cancel(cancel)
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink() or (options.skip_hidden and entry.name.startswith(".")):
                continue
            if Path(directory) == root and entry.name == QUARANTINE_DIR:
                continue
            if _excluded(entry.path, folders):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat_module.S_ISDIR(info.st_mode):
                pending.append(entry.path)
                continue
            if not stat_module.S_ISREG(info.st_mode):
                continue
            extension = os.path.splitext(entry.name)[1].casefold()
            if options.include_extensions and extension not in options.include_extensions:
                continue
            if extension in options.exclude_extensions:
                continue
            if info.st_size < options.min_size or (options.max_size is not None and info.st_size > options.max_size):
                continue
            track = tracks.get(entry.path)
            if track is not None and (track.size, track.mtime_ns) != (info.st_size, info.st_mtime_ns):
                track = None
            records.append(FileRecord(entry.path, info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino, track,
                                      info.st_nlink))
        if progress:
            progress(0, 0, f"Inventaire des fichiers : {directory}")
    records.sort(key=lambda record: record.path)
    return records


def _union(groups: list[list[FileRecord]], pairs) -> list[list[FileRecord]]:
    parent = {id(record): id(record) for group in groups for record in group}
    by_id = {id(record): record for group in groups for record in group}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    for left, right in pairs:
        parent[find(id(left))] = find(id(right))
    clusters = defaultdict(list)
    for key in parent:
        clusters[find(key)].append(by_id[key])
    return [sorted(cluster, key=lambda record: record.path) for cluster in clusters.values() if len(cluster) > 1]


def _split_by_duration(bucket: list[FileRecord], tolerance: float) -> list[list[FileRecord]]:
    """Groupes de durées proches, en un seul passage trié : chaque groupe tient dans la
    tolérance mesurée depuis sa plus courte durée. Les écarts ne s'additionnent plus de proche
    en proche (studio 446 s, live 453 s : groupes distincts)."""
    timed = sorted((record for record in bucket if record.duration), key=lambda record: record.duration)
    clusters: list[list[FileRecord]] = []
    for record in timed:
        if clusters and record.duration - clusters[-1][0].duration <= tolerance:
            clusters[-1].append(record)
        else:
            clusters.append([record])
    return [sorted(cluster, key=lambda record: record.path) for cluster in clusters if len(cluster) > 1]


# Au-delà, une recherche par empreinte durerait des heures : elle est refusée avec un conseil.
MAX_FINGERPRINT_PAIRS = 1_000_000


def _fingerprint_pairs(groups: list[list[FileRecord]], window: float = 5.0) -> int:
    """Comparaisons nécessaires (fenêtre de durée de 5 s), comptées sans les faire."""
    total = 0
    for group in groups:
        durations = sorted(record.duration or 0 for record in group)
        start = 0
        for index, duration in enumerate(durations):
            while duration - durations[start] > window:
                start += 1
            total += index - start
    return total


def find_duplicates(records: list[FileRecord], options: ScanOptions, cache: Cache | None = None,
                    fingerprinter=None, cancel: Event | None = None, progress: Progress = None) -> list[FileGroup]:
    criteria = options.criteria
    audio_only = SONG in criteria or FINGERPRINT in criteria
    if audio_only:
        needed = [record for record in records if record.extension in AUDIO_EXTENSIONS]
        for position, record in enumerate(needed, 1):
            check_cancel(cancel)
            if record.track is None:
                try:
                    record.track = read_track(Path(record.path))
                except (OSError, ValueError, TypeError):
                    record.track = None
            if progress and position % 50 == 0:
                progress(position, len(needed), f"Lecture des tags : {record.path}")
        records = [record for record in needed if record.track is not None]

    def key(record: FileRecord):
        parts = []
        if NAME in criteria:
            # NFC : « Café » écrit par un Mac (accent décomposé) égale « Café » écrit ailleurs.
            parts.append(unicodedata.normalize("NFC", record.name).casefold())
        if SIZE in criteria or CONTENT in criteria:
            parts.append(record.size)
        if DATE in criteria:
            parts.append(record.mtime_ns // 1_000_000_000)
        if SONG in criteria:
            artist = name_key(record.track.value("artist") or record.track.value("albumartist"))
            title = name_key(record.track.value("title"))
            if not artist or not title:
                return None
            parts.extend((artist, title))
        return tuple(parts)

    buckets = defaultdict(list)
    for record in records:
        value = key(record)
        if value is not None:
            buckets[value].append(record)
    groups = [bucket for bucket in buckets.values() if len(bucket) > 1]
    if SONG in criteria:
        groups = [part for bucket in groups for part in _split_by_duration(bucket, options.duration_tolerance)]
    # Un fichier lié physiquement n'est lu qu'une fois : ses autres chemins deviennent des alias
    # avant les étapes coûteuses (SHA-256, empreinte), qui relisaient chaque chemin sur le NAS.
    if CONTENT in criteria or FINGERPRINT in criteria:
        groups = [merged for group in groups if len(merged := _merge_links(group)) > 1]
    if CONTENT in criteria:
        if cache is None:
            raise ValueError("Le cache est nécessaire pour comparer le contenu.")
        total = sum(len(group) for group in groups)
        done, split = 0, []
        for group in groups:
            by_hash = defaultdict(list)
            for record in group:
                check_cancel(cancel)
                try:
                    digest = cache.hash_track(Track(record.path, size=record.size, mtime_ns=record.mtime_ns), cancel)
                except OSError:
                    digest = None
                if digest:
                    by_hash[digest].append(record)
                done += 1
                if progress:
                    progress(done, total, f"SHA-256 : {record.path}")
            split.extend(part for part in by_hash.values() if len(part) > 1)
        groups = split
    if FINGERPRINT in criteria:
        if fingerprinter is None:
            raise ValueError("Chromaprint est nécessaire pour comparer les empreintes.")
        from .recognition import FeatureUnavailable, fingerprint_similarity, pack_fingerprint
        estimated = _fingerprint_pairs(groups)
        if estimated > MAX_FINGERPRINT_PAIRS:
            raise ValueError(f"Empreinte audio : environ {estimated:,} comparaisons nécessaires, ce qui prendrait "
                             "des heures. Combinez-la avec « Même morceau » ou « Taille », ou excluez des dossiers."
                             .replace(",", " "))
        total = sum(len(group) for group in groups)
        done, raw = 0, {}
        for group in groups:
            for record in group:
                check_cancel(cancel)
                try:
                    raw[id(record)] = fingerprinter.raw(record.track, cancel)
                except (OSError, ValueError, FeatureUnavailable):
                    pass  # fichier que fpcalc ne décode pas : ignoré, la recherche continue
                done += 1
                if progress:
                    progress(done, total, f"Empreinte audio : {record.path}")
        packed = {key: (duration, pack_fingerprint(values)) for key, (duration, values) in raw.items()}
        refined = []
        for group in groups:
            timed = sorted((record for record in group if id(record) in packed), key=lambda record: packed[id(record)][0])
            # Chaque fichier rejoint le premier groupe dont le fichier de référence lui ressemble :
            # A≈B et B≈C ne suffisent plus à réunir A et C (versions live différentes).
            clusters: list[list[FileRecord]] = []
            for record in timed:
                duration, values = packed[id(record)]
                for cluster in clusters:
                    seed_duration, seed_values = packed[id(cluster[0])]
                    if abs(duration - seed_duration) <= 5 and \
                            fingerprint_similarity(seed_values, values) >= options.fingerprint_threshold:
                        cluster.append(record)
                        break
                else:
                    clusters.append([record])
            refined.extend(sorted(cluster, key=lambda record: record.path) for cluster in clusters if len(cluster) > 1)
        groups = refined
    order = tuple(criterion for criterion in CRITERIA if criterion in criteria)
    result = []
    for group in groups:
        physical = _merge_links(group)
        if len(physical) > 1:
            result.append(FileGroup(physical, order))
    result.sort(key=lambda group: (-group.recoverable_bytes, group.files[0].path))
    return result


def _merge_links(group: list[FileRecord]) -> list[FileRecord]:
    """Les chemins d'un même fichier physique (même périphérique et inode) ne sont pas des doublons :
    un seul est gardé dans le groupe, les autres sont listés comme alias."""
    by_identity: dict[tuple, FileRecord] = {}
    for record in sorted(group, key=lambda record: record.path):
        # Inode 0 : identité inconnue (Windows, certains montages) ; chaque chemin reste distinct.
        key = (record.device, record.inode) if record.inode else ("chemin", record.path)
        if key in by_identity:
            by_identity[key].aliases.append(record.path)
        else:
            by_identity[key] = record
    return sorted(by_identity.values(), key=lambda record: record.path)


# Aides de sélection : renvoient l'ensemble des chemins à cocher.
# Un fichier lié physiquement n'est jamais coché automatiquement : le retirer ne libère pas
# d'espace et peut casser le partage qBittorrent ou la bibliothèque Lidarr.
def select_all_but_best(groups: list[FileGroup]) -> set[str]:
    chosen = set()
    for group in groups:
        best = group.best()
        chosen |= {record.path for record in group.files if record is not best and not record.linked
                   and not record.seeded}
    return chosen


def select_lossy_when_lossless(groups: list[FileGroup]) -> set[str]:
    chosen = set()
    for group in groups:
        if any(record.codec in LOSSLESS for record in group.files):
            chosen |= {record.path for record in group.files
                       if record.track is not None and record.codec not in LOSSLESS and not record.linked
                       and not record.seeded}
    return chosen


def select_keep_by_date(groups: list[FileGroup], newest: bool) -> set[str]:
    chosen = set()
    for group in groups:
        keep = (max if newest else min)(group.files, key=lambda record: (record.mtime_ns, record.path))
        chosen |= {record.path for record in group.files if record is not keep and not record.linked
                   and not record.seeded}
    return chosen


def fully_selected(groups: list[FileGroup], selected: set[str]) -> list[FileGroup]:
    """Groupes dont toutes les copies sont cochées : refusés pour ne rien perdre."""
    return [group for group in groups if all(record.path in selected for record in group.files)]


class StaleSelection(RuntimeError):
    """La liste de groupes ne correspond plus au disque."""


def _unchanged(record: FileRecord) -> bool:
    try:
        info = os.lstat(record.path)
    except OSError:
        return False
    return stat_module.S_ISREG(info.st_mode) and (info.st_size, info.st_mtime_ns) == (record.size, record.mtime_ns)


def groups_without_kept_copy(groups: list[FileGroup], selected: set[str]) -> list[FileGroup]:
    """Groupes touchés par la sélection dont aucune copie non cochée n'existe plus telle
    qu'analysée (déplacée par une quarantaine d'albums, renommée, modifiée) : les retirer
    ferait disparaître toutes les copies de la bibliothèque. Lit le disque : à appeler hors du fil Qt."""
    orphaned = []
    for group in groups:
        if not any(record.path in selected for record in group.files):
            continue
        if not any(_unchanged(record) for record in group.files if record.path not in selected):
            orphaned.append(group)
    return orphaned


# Quarantaine des fichiers, journal et restauration.
def _name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _destination(root: Path, source: Path, listings: dict[Path, set[str]] | None = None) -> Path:
    """Emplacement libre en quarantaine, insensible à la casse et à la forme Unicode.

    `listings` garde le contenu de chaque dossier lu une seule fois pendant un lot, et les noms
    déjà réservés ; sans lui, chaque fichier relisait tout le dossier de destination."""
    relative = source.relative_to(root)
    folder = root / QUARANTINE_DIR / relative.parent
    if listings is None:
        listings = {}
    names = listings.get(folder)
    if names is None:
        try:
            names = {_name_key(name) for name in os.listdir(folder)}
        except FileNotFoundError:
            names = set()
        listings[folder] = names
    destination = folder / source.name
    suffix = 2
    while _name_key(destination.name) in names or os.path.lexists(destination):
        destination = folder / f"{source.stem} ({suffix}){source.suffix}"
        suffix += 1
    names.add(_name_key(destination.name))
    return destination


FILE_JOURNAL_BATCH = 64
MAX_FILE_JOURNAL_BYTES = 256 * 1024 * 1024  # ≈ 350 000 fichiers


def quarantine_files(records: list[FileRecord], library_root: str | Path, journal_dir: str | Path,
                     cancel: Event | None = None, progress: Progress = None) -> tuple[Path, list[str], list[tuple[str, str]]]:
    """Déplace chaque fichier inchangé depuis l'analyse ; journal écrit avant chaque déplacement.

    Une annulation arrête la boucle sans lever d'exception : les fichiers déjà déplacés sont
    renvoyés, pour que l'interface reflète l'état réel du disque."""
    root = Path(library_root).absolute()
    journal_dir = Path(journal_dir)
    _mkdir_safe(journal_dir)
    journal = journal_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-fichiers-" + uuid4().hex + ".json")
    record = {"version": FILE_JOURNAL_VERSION, "kind": "files", "root": str(root),
              "created_at": datetime.now(timezone.utc).isoformat(), "entries": []}
    moved, failed = [], []
    # Par lots : les intentions d'un lot sont enregistrées (fsync) avant ses déplacements, puis
    # leurs états après. Avant, tout le journal était réécrit deux fois par fichier (coût
    # quadratique : 3 Go écrits pour 2 000 fichiers).
    position = 0
    listings: dict[Path, set[str]] = {}
    for start in range(0, len(records), FILE_JOURNAL_BATCH):
        if cancel is not None and cancel.is_set():
            break
        prepared = []
        for item in records[start:start + FILE_JOURNAL_BATCH]:
            source = Path(item.path).absolute()
            try:
                if not source.is_relative_to(root) or source.relative_to(root).parts[0] == QUARANTINE_DIR:
                    raise OrganizeError("Fichier hors de la racine ou déjà en quarantaine.")
                _check_ancestors(source.parent)
                info = source.lstat()
                if not stat_module.S_ISREG(info.st_mode):
                    raise OrganizeError("Ce n'est pas un fichier ordinaire.")
                # Sans identité connue à l'analyse (inode 0, Windows), seules taille et date sont comparées.
                identity = (info.st_dev, info.st_ino) if item.inode else (item.device, item.inode)
                if (info.st_size, info.st_mtime_ns) + identity != (item.size, item.mtime_ns, item.device, item.inode):
                    raise OrganizeError("Fichier modifié depuis l'analyse : relancez la recherche.")
                destination = _destination(root, source, listings)
                _mkdir_safe(destination.parent)
                nearest = _check_ancestors(destination.parent)
                if nearest.stat().st_dev != info.st_dev:
                    raise OrganizeError("Déplacement entre volumes refusé.")
                entry = {"source": str(source), "destination": str(destination), "size": info.st_size,
                         "mtime_ns": info.st_mtime_ns, "state": "intent"}
                prepared.append((source, entry, info))
            except (OSError, OrganizeError) as exc:
                failed.append((str(source), str(exc)))
        if not prepared:
            continue
        record["entries"].extend(entry for _, entry, _ in prepared)
        _save_journal(journal, record)
        try:
            for source, entry, info in prepared:
                if cancel is not None and cancel.is_set():
                    break  # les intentions restantes sont reconnues comme « jamais déplacées »
                try:
                    _rename_without_replace(source, Path(entry["destination"]), (info.st_dev, info.st_ino),
                                            is_dir=False)
                    entry["state"] = "moved"
                    moved.append(str(source))
                except (OSError, OrganizeError) as exc:
                    # État laissé à « intent » : la restauration vérifie sur le disque si le fichier
                    # est parti (échec après renommage) ou resté en place.
                    entry["error"] = str(exc)
                    failed.append((str(source), str(exc)))
                position += 1
                if progress:
                    progress(position, len(records), str(source))
        finally:
            _save_journal(journal, record)
    if not record["entries"]:
        _save_journal(journal, record)
    return journal, moved, failed


def read_file_journal(journal_path: str | Path) -> dict:
    path = Path(journal_path)
    if path.is_symlink() or path.stat().st_size > MAX_FILE_JOURNAL_BYTES:
        raise ValueError("Journal lié ou trop volumineux")
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, dict) or data.get("version") != FILE_JOURNAL_VERSION or data.get("kind") != "files":
        raise ValueError("Ce n'est pas un journal de fichiers en quarantaine")
    if not isinstance(data.get("entries"), list):
        raise ValueError("Journal invalide")
    return data


def restore_file_journal(journal_path: str | Path, cancel: Event | None = None,
                         progress: Progress = None) -> tuple[list[str], list[tuple[str, str]]]:
    """Remet chaque fichier à sa place s'il est intact en quarantaine et que sa place est libre.

    Le journal est enregistré régulièrement et en toute fin, même après une annulation ou une
    erreur : une reprise ne voit jamais un fichier déjà revenu comme « encore en quarantaine »."""
    path = Path(journal_path)
    data = read_file_journal(path)
    restored, failed = [], []
    entries = [entry for entry in data["entries"] if entry.get("state") in {"moved", "intent"}]
    try:
        for position, entry in enumerate(entries, 1):
            if cancel is not None and cancel.is_set():
                break
            try:
                source, destination = Path(entry["source"]), Path(entry["destination"])
                if entry["state"] == "intent" and source.exists() and destination.exists() and entry["size"] > 0:
                    # Quarantaine interrompue pendant le repli SMB/NFS : seule la réservation vide existe.
                    _remove_reservation(destination)
                if not destination.exists() and source.exists() and entry["state"] == "intent":
                    entry["state"] = "restored"  # déplacement jamais effectué
                    continue
                _check_ancestors(destination.parent)
                info = destination.lstat()
                if not stat_module.S_ISREG(info.st_mode) or (info.st_size, info.st_mtime_ns) != (entry["size"], entry["mtime_ns"]):
                    raise OrganizeError("Le fichier en quarantaine a changé ou a disparu.")
                if _collision(source) and entry["size"] > 0:
                    # Restauration interrompue pendant le repli SMB/NFS : réservation vide à l'origine.
                    _remove_reservation(source)
                if _collision(source):
                    raise OrganizeError("Un fichier occupe déjà l'emplacement d'origine.")
                _mkdir_safe(source.parent)
                _rename_without_replace(destination, source, (info.st_dev, info.st_ino), is_dir=False)
                entry["state"] = "restored"
                restored.append(str(source))
            except (OSError, OrganizeError, KeyError, TypeError) as exc:
                failed.append((str(entry.get("source", "?")), str(exc)))
            if progress:
                progress(position, len(entries), str(entry.get("source", "")))
            if position % 50 == 0:
                _save_journal(path, data)
    finally:
        _save_journal(path, data)
    return restored, failed


def quarantine_summary(library_root: str | Path) -> tuple[Path, int, int]:
    """Dossier de quarantaine, nombre de fichiers et octets qu'il contient."""
    folder = Path(library_root).absolute() / QUARANTINE_DIR
    count = size = 0
    if folder.is_dir() and not folder.is_symlink():
        for current, directories, files in os.walk(folder):
            for name in files:
                try:
                    info = os.lstat(os.path.join(current, name))
                except OSError:
                    continue
                if stat_module.S_ISREG(info.st_mode):
                    count += 1
                    size += info.st_size
    return folder, count, size


def empty_quarantine(library_root: str | Path, confirmed: bool = False) -> tuple[int, int]:
    """Suppression définitive du contenu de la quarantaine, et seulement de lui."""
    if not confirmed:
        raise OrganizeError("Une confirmation explicite est nécessaire pour vider la quarantaine.")
    folder, count, size = quarantine_summary(library_root)
    if not folder.exists():
        return 0, 0
    if folder.is_symlink() or not folder.is_dir() or folder.parent != Path(library_root).absolute():
        raise OrganizeError("Dossier de quarantaine inattendu : suppression refusée.")
    if not shutil.rmtree.avoids_symlink_attacks:
        raise OrganizeError("Suppression sûre indisponible sur ce système.")
    shutil.rmtree(folder)
    _fsync_directory(folder.parent)
    return count, size
