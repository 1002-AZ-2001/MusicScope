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
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import stat as stat_module
from threading import Event
from typing import Callable
from uuid import uuid4

from .cache import Cache, check_cancel
from .metadata import AUDIO_EXTENSIONS, read_track
from .models import Track
from .normalization import name_key
from .organizer import (
    QUARANTINE_DIR, OrganizeError, _check_ancestors, _collision, _fsync_directory, _mkdir_safe,
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
        return len({(record.device, record.inode) for record in self.files}) < len(self.files)

    def best(self) -> FileRecord:
        return max(self.files, key=quality_key)


def quality_key(record: FileRecord) -> tuple:
    """Meilleure copie : sans perte, débit, tags complets, chemin le moins profond."""
    track = record.track
    tags = sum(bool(track.value(key)) for key in ("artist", "title", "album", "tracknumber")) if track else 0
    return (CODEC_RANK.get(record.codec, 0), record.bitrate or 0, tags, -record.path.count(os.sep), -len(record.path),
            record.path)


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
            records.append(FileRecord(entry.path, info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino, track))
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
    timed = sorted((record for record in bucket if record.duration), key=lambda record: record.duration)
    pairs = [(a, b) for index, a in enumerate(timed) for b in timed[index + 1:]
             if b.duration - a.duration <= tolerance] if timed else []
    return _union([timed], pairs)


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
            parts.append(record.name.casefold())
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
        from .recognition import fingerprint_similarity
        total = sum(len(group) for group in groups)
        done, raw = 0, {}
        for group in groups:
            for record in group:
                check_cancel(cancel)
                try:
                    raw[id(record)] = fingerprinter.raw(record.track, cancel)
                except OSError:
                    pass
                done += 1
                if progress:
                    progress(done, total, f"Empreinte audio : {record.path}")
        refined = []
        for group in groups:
            timed = sorted((record for record in group if id(record) in raw), key=lambda record: raw[id(record)][0])
            pairs = []
            for index, left in enumerate(timed):
                for right in timed[index + 1:]:
                    if raw[id(right)][0] - raw[id(left)][0] > 5:
                        break
                    if fingerprint_similarity(raw[id(left)][1], raw[id(right)][1]) >= options.fingerprint_threshold:
                        pairs.append((left, right))
            refined.extend(_union([timed], pairs))
        groups = refined
    order = tuple(criterion for criterion in CRITERIA if criterion in criteria)
    result = [FileGroup(sorted(group, key=lambda record: record.path), order) for group in groups]
    result.sort(key=lambda group: (-sum(record.size for record in group.files[1:]), group.files[0].path))
    return result


# Aides de sélection : renvoient l'ensemble des chemins à cocher.
def select_all_but_best(groups: list[FileGroup]) -> set[str]:
    return {record.path for group in groups for record in group.files if record is not group.best()}


def select_lossy_when_lossless(groups: list[FileGroup]) -> set[str]:
    chosen = set()
    for group in groups:
        if any(record.codec in LOSSLESS for record in group.files):
            chosen |= {record.path for record in group.files
                       if record.track is not None and record.codec not in LOSSLESS}
    return chosen


def select_keep_by_date(groups: list[FileGroup], newest: bool) -> set[str]:
    chosen = set()
    for group in groups:
        keep = (max if newest else min)(group.files, key=lambda record: (record.mtime_ns, record.path))
        chosen |= {record.path for record in group.files if record is not keep}
    return chosen


def fully_selected(groups: list[FileGroup], selected: set[str]) -> list[FileGroup]:
    """Groupes dont toutes les copies sont cochées : refusés pour ne rien perdre."""
    return [group for group in groups if all(record.path in selected for record in group.files)]


# Quarantaine des fichiers, journal et restauration.
def _destination(root: Path, source: Path) -> Path:
    relative = source.relative_to(root)
    destination = root / QUARANTINE_DIR / relative
    suffix = 2
    while _collision(destination):
        destination = root / QUARANTINE_DIR / relative.parent / f"{source.stem} ({suffix}){source.suffix}"
        suffix += 1
    return destination


def quarantine_files(records: list[FileRecord], library_root: str | Path, journal_dir: str | Path,
                     cancel: Event | None = None, progress: Progress = None) -> tuple[Path, list[str], list[tuple[str, str]]]:
    """Déplace chaque fichier inchangé depuis l'analyse ; journal écrit avant chaque déplacement."""
    root = Path(library_root).absolute()
    journal_dir = Path(journal_dir)
    _mkdir_safe(journal_dir)
    journal = journal_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-fichiers-" + uuid4().hex + ".json")
    record = {"version": FILE_JOURNAL_VERSION, "kind": "files", "root": str(root),
              "created_at": datetime.now(timezone.utc).isoformat(), "entries": []}
    moved, failed = [], []
    for position, item in enumerate(records, 1):
        check_cancel(cancel)
        source = Path(item.path).absolute()
        try:
            if not source.is_relative_to(root) or source.relative_to(root).parts[0] == QUARANTINE_DIR:
                raise OrganizeError("Fichier hors de la racine ou déjà en quarantaine.")
            _check_ancestors(source.parent)
            info = source.lstat()
            if not stat_module.S_ISREG(info.st_mode):
                raise OrganizeError("Ce n'est pas un fichier ordinaire.")
            if (info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino) != (item.size, item.mtime_ns, item.device, item.inode):
                raise OrganizeError("Fichier modifié depuis l'analyse : relancez la recherche.")
            destination = _destination(root, source)
            _mkdir_safe(destination.parent)
            nearest = _check_ancestors(destination.parent)
            if nearest.stat().st_dev != info.st_dev:
                raise OrganizeError("Déplacement entre volumes refusé.")
            entry = {"source": str(source), "destination": str(destination), "size": info.st_size,
                     "mtime_ns": info.st_mtime_ns, "state": "intent"}
            record["entries"].append(entry)
            _save_journal(journal, record)
            _rename_without_replace(source, destination, (info.st_dev, info.st_ino), is_dir=False)
            entry["state"] = "moved"
            _save_journal(journal, record)
            moved.append(str(source))
        except (OSError, OrganizeError) as exc:
            failed.append((str(source), str(exc)))
        if progress:
            progress(position, len(records), str(source))
    if not record["entries"]:
        _save_journal(journal, record)
    return journal, moved, failed


def read_file_journal(journal_path: str | Path) -> dict:
    path = Path(journal_path)
    if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Journal lié ou trop volumineux")
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, dict) or data.get("version") != FILE_JOURNAL_VERSION or data.get("kind") != "files":
        raise ValueError("Ce n'est pas un journal de fichiers en quarantaine")
    if not isinstance(data.get("entries"), list):
        raise ValueError("Journal invalide")
    return data


def restore_file_journal(journal_path: str | Path, cancel: Event | None = None,
                         progress: Progress = None) -> tuple[list[str], list[tuple[str, str]]]:
    """Remet chaque fichier à sa place s'il est intact en quarantaine et que sa place est libre."""
    path = Path(journal_path)
    data = read_file_journal(path)
    restored, failed = [], []
    entries = [entry for entry in data["entries"] if entry.get("state") in {"moved", "intent"}]
    for position, entry in enumerate(entries, 1):
        check_cancel(cancel)
        try:
            source, destination = Path(entry["source"]), Path(entry["destination"])
            if not destination.exists() and source.exists() and entry["state"] == "intent":
                entry["state"] = "restored"  # déplacement jamais effectué
                continue
            _check_ancestors(destination.parent)
            info = destination.lstat()
            if not stat_module.S_ISREG(info.st_mode) or (info.st_size, info.st_mtime_ns) != (entry["size"], entry["mtime_ns"]):
                raise OrganizeError("Le fichier en quarantaine a changé ou a disparu.")
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
