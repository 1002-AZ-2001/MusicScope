"""Rangement assisté explicite : un renommage atomique, sans remplacement.

La prévisualisation ne modifie aucune source. L'application et la restauration
exigent une confirmation indépendante ; elles ne réécrivent jamais les tags et
ne copient/suppriment jamais les fichiers pour traverser un volume.
"""
from __future__ import annotations

import ctypes
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import unicodedata
from uuid import uuid4

from .cache import check_cancel
from .metadata import AUDIO_EXTENSIONS
from .models import Album

JOURNAL_VERSION = 1
MAX_ENTRIES = 100_000


class OrganizeError(RuntimeError):
    def __init__(self, message: str, journal_path: Path | None = None):
        super().__init__(message)
        self.journal_path = journal_path


@dataclass(frozen=True)
class FileEntry:
    relative_path: str
    kind: str
    size: int
    mtime_ns: int
    device: int
    inode: int
    nlink: int = 1
    sha256: str = ""


@dataclass
class MovePlan:
    source: str
    destination: str
    files: list[FileEntry] = field(default_factory=list)
    fingerprint: str = ""
    warnings: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    hardlinked_files: int = 0
    source_device: int = 0
    source_inode: int = 0


def sanitize_component(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", text).strip().rstrip(". ")
    if not text or text in {".", ".."}:
        text = "_"
    if re.fullmatch(r"(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", text, flags=re.I):
        text = "_" + text
    return text[:120].rstrip(". ")


def _absolute(path: str | Path) -> Path:
    return Path(os.path.abspath(Path(path).expanduser()))


def _is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _check_ancestors(path: Path, *, allow_missing: bool = False) -> Path:
    """Refuse aussi les liens dans les parents, pas seulement le dernier nom."""
    parts = [path, *path.parents]
    existing = None
    for candidate in reversed(parts):
        if _is_link(candidate):
            raise OrganizeError(f"Lien symbolique ou jonction interdit : {candidate}")
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if allow_missing:
                continue
            raise OrganizeError(f"Dossier inaccessible : {candidate}") from None
        if not stat.S_ISDIR(info.st_mode):
            raise OrganizeError(f"Un parent n'est pas un dossier : {candidate}")
        existing = candidate
    if existing is None:
        raise OrganizeError(f"Aucun parent accessible : {path}")
    return existing


def _hash_file(path: Path, initial: os.stat_result, cancel=None) -> str:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if _version(initial) != _version(opened) or not stat.S_ISREG(opened.st_mode):
            raise OrganizeError(f"Fichier remplacé pendant la lecture : {path}")
        digest = hashlib.sha256()
        while block := stream.read(1024 * 1024):
            check_cancel(cancel)
            digest.update(block)
        final = os.fstat(stream.fileno())
    if _is_link(path) or _version(initial) != _version(final) or _version(initial) != _version(path.lstat()):
        raise OrganizeError(f"Fichier modifié pendant la prévisualisation : {path}")
    return digest.hexdigest()


def _version(info):
    return info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino


def _snapshot(source: Path, cancel=None) -> list[FileEntry]:
    _check_ancestors(source)
    entries: list[FileEntry] = []
    pending = [source]
    discovered = 1
    while pending:
        check_cancel(cancel)
        folder = pending.pop()
        if _is_link(folder):
            raise OrganizeError(f"Lien ou jonction interdit : {folder}")
        initial = folder.lstat()
        if not stat.S_ISDIR(initial.st_mode):
            raise OrganizeError(f"Dossier remplacé pendant la prévisualisation : {folder}")
        entries.append(FileEntry(str(folder.relative_to(source)), "directory", 0,
                                 initial.st_mtime_ns, initial.st_dev, initial.st_ino, initial.st_nlink))
        with os.scandir(folder) as iterator:
            for item in iterator:
                check_cancel(cancel)
                discovered += 1
                if discovered > MAX_ENTRIES:
                    raise OrganizeError("Dossier trop volumineux pour un déplacement assisté unique.")
                path = Path(item.path)
                if _is_link(path):
                    raise OrganizeError(f"Lien ou jonction interdit : {path}")
                info = item.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                elif stat.S_ISREG(info.st_mode):
                    digest = _hash_file(path, info, cancel)
                    entries.append(FileEntry(str(path.relative_to(source)), "file", info.st_size,
                                             info.st_mtime_ns, info.st_dev, info.st_ino, info.st_nlink, digest))
                else:
                    raise OrganizeError(f"Fichier spécial interdit : {path}")
        final = folder.lstat()
        if (initial.st_mtime_ns, initial.st_dev, initial.st_ino) != (final.st_mtime_ns, final.st_dev, final.st_ino):
            raise OrganizeError(f"Dossier modifié pendant la prévisualisation : {folder}")
    return sorted(entries, key=lambda item: item.relative_path)


def _fingerprint(entries: list[FileEntry]) -> str:
    payload = json.dumps([asdict(entry) for entry in entries], sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _collision(path: Path) -> bool:
    if path.exists() or path.is_symlink():
        return True
    if not path.parent.exists():
        return False
    key = unicodedata.normalize("NFC", path.name).casefold()
    return any(unicodedata.normalize("NFC", entry.name).casefold() == key for entry in path.parent.iterdir())


def plan_album(album: Album, destination_root: str | Path, cancel=None) -> MovePlan:
    source = _absolute(album.path)
    root = _absolute(destination_root)
    plan = MovePlan(str(source), "")
    if not album.complete or not album.tracks:
        plan.blockers.append("L'inventaire de l'album doit être complet et non vide.")
    for field_name, label in (("albumartist", "Artiste d'album"), ("album", "Titre d'album")):
        signatures = album.tag_signatures(field_name)
        if len(signatures) != 1 or any(not track.value(field_name) for track in album.tracks):
            plan.blockers.append(f"{label} absent ou incohérent : corriger les tags avant de ranger.")
    if any("regroupement ambigu" in issue.casefold() for issue in album.issues):
        plan.blockers.append("Le regroupement de l'album est ambigu.")
    dates = album.values("date")
    if len(dates) > 1:
        plan.blockers.append("Dates d'édition incohérentes.")
    year = "Année inconnue"
    if len(dates) == 1 and re.match(r"^[12]\d{3}(?:\D|$)", next(iter(dates))):
        year = next(iter(dates))[:4]
    else:
        plan.warnings.append("Date d'édition absente ou invalide : année inconnue dans le nom proposé.")
    artist, title = album.value("albumartist"), album.value("album")
    artist_name, album_name = sanitize_component(artist), sanitize_component(f"{title} ({year})")
    if artist_name != artist or album_name != f"{title} ({year})":
        plan.warnings.append("Certains caractères ont été adaptés pour produire des noms compatibles Windows/Linux.")
    destination = root / artist_name / album_name
    plan.destination = str(destination)
    try:
        _check_ancestors(source)
        nearest = _check_ancestors(destination.parent, allow_missing=True)
        info = source.lstat()
        plan.source_device, plan.source_inode = info.st_dev, info.st_ino
        if source == destination:
            raise OrganizeError("Le dossier utilise déjà le chemin proposé.")
        if destination.is_relative_to(source) or source.is_relative_to(destination):
            raise OrganizeError("La destination ne doit ni contenir la source ni être contenue dans la source.")
        if _collision(destination):
            raise OrganizeError(f"La destination existe déjà (casse et Unicode compris) : {destination}")
        if info.st_dev != nearest.stat().st_dev:
            raise OrganizeError("Déplacement entre volumes refusé : choisir une destination sur le même volume.")
        plan.files = _snapshot(source, cancel)
        root_entry = next(entry for entry in plan.files if entry.relative_path == ".")
        if (root_entry.device, root_entry.inode) != (plan.source_device, plan.source_inode):
            raise OrganizeError("Le dossier source a été remplacé pendant la prévisualisation.")
        plan.fingerprint = _fingerprint(plan.files)
        inventoried_paths = {_absolute(track.path) for track in album.tracks}
        other_audio = [source / entry.relative_path for entry in plan.files
                       if entry.kind == "file" and Path(entry.relative_path).suffix.casefold() in AUDIO_EXTENSIONS
                       and source / entry.relative_path not in inventoried_paths]
        if other_audio:
            raise OrganizeError("Le dossier contient d'autres fichiers audio hors de cet album inventorié : "
                                "sélectionner un dossier d'album distinct avant de le déplacer. "
                                + "; ".join(str(path.relative_to(source)) for path in other_audio[:5]))
        plan.hardlinked_files = sum(entry.kind == "file" and entry.nlink > 1 for entry in plan.files)
        if plan.hardlinked_files:
            plan.warnings.append(f"{plan.hardlinked_files} fichier(s) lié(s) physiquement : accord distinct requis. "
                                 "Le renommage du dossier conservera les liens ; aucun fichier n'est réécrit.")
        for track in album.tracks:
            path = _absolute(track.path)
            if not path.is_relative_to(source):
                raise OrganizeError("Une piste de l'inventaire est située hors du dossier d'album.")
            current = path.lstat()
            if ((track.size, track.mtime_ns) != (current.st_size, current.st_mtime_ns)
                    or (track.device is not None and track.inode is not None
                        and (track.device, track.inode) != (current.st_dev, current.st_ino))):
                raise OrganizeError(f"Inventaire ancien : analyser à nouveau avant de déplacer {path.name}.")
    except (OSError, OrganizeError) as exc:
        plan.blockers.append(str(exc))
    return plan


def _fsync_directory(path: Path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _save_journal(path: Path, record: dict):
    temp = path.with_name(path.name + ".tmp-" + uuid4().hex)
    try:
        with temp.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=True, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        _fsync_directory(path.parent)
    finally:
        if temp.exists():
            temp.unlink()


def _open_directory(path: Path, *, create=False) -> int:
    """Sous Linux, ouvrir chaque parent avec O_NOFOLLOW pour lier le renommage
    aux bons dossiers même si un autre processus renomme leurs ancêtres.
    """
    descriptor = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            if create:
                try:
                    os.mkdir(part, dir_fd=descriptor)
                except FileExistsError:
                    pass
            next_descriptor = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _rename_without_replace(source: Path, destination: Path, expected: tuple[int, int]):
    if os.name == "nt":
        _check_ancestors(source.parent)
        _check_ancestors(destination.parent)
        info = source.lstat()
        if _is_link(source) or (info.st_dev, info.st_ino) != expected:
            raise OrganizeError("Le dossier source a été remplacé.")
        os.rename(source, destination)  # Windows refuse un nom déjà présent.
        return
    if not sys.platform.startswith("linux"):
        raise OrganizeError("Le renommage atomique sans remplacement est pris en charge sous Linux et Windows.")
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is None:
        raise OrganizeError("renameat2 sans remplacement indisponible sur ce système.")
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    source_fd = _open_directory(source.parent)
    try:
        destination_fd = _open_directory(destination.parent)
        try:
            info = os.stat(source.name, dir_fd=source_fd, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != expected:
                raise OrganizeError("Le dossier source a été remplacé.")
            if rename(source_fd, os.fsencode(source.name), destination_fd, os.fsencode(destination.name), 1):
                code = ctypes.get_errno()
                raise OSError(code, os.strerror(code), str(destination))
            os.fsync(source_fd)
            os.fsync(destination_fd)
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)


def _mkdir_safe(path: Path):
    if sys.platform.startswith("linux"):
        descriptor = _open_directory(path, create=True)
        os.close(descriptor)
    else:
        _check_ancestors(path, allow_missing=True)
        path.mkdir(parents=True, exist_ok=True)
        _check_ancestors(path)


def _journal_folder(path: str | Path, source: Path, destination: Path) -> Path:
    folder = _absolute(path)
    if folder.is_relative_to(source) or folder.is_relative_to(destination):
        raise OrganizeError("Le journal doit être placé hors des dossiers déplacés.")
    _check_ancestors(folder, allow_missing=True)
    return folder


def apply_plan(plan: MovePlan, journal_dir: str | Path, confirmed: bool = False,
               allow_hardlinks: bool = False) -> Path:
    if not confirmed:
        raise OrganizeError("Une confirmation manuelle de la prévisualisation est obligatoire.")
    if plan.blockers:
        raise OrganizeError("Plan bloqué : " + " ; ".join(plan.blockers))
    if plan.hardlinked_files and not allow_hardlinks:
        raise OrganizeError("Confirmer séparément la conservation des liens physiques avant d'appliquer ce plan.")
    source, destination = _absolute(plan.source), _absolute(plan.destination)
    if source == destination or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise OrganizeError("Relation source/destination interdite.")
    folder = _journal_folder(journal_dir, source, destination)
    _check_ancestors(source)
    nearest = _check_ancestors(destination.parent, allow_missing=True)
    if source.stat().st_dev != nearest.stat().st_dev:
        raise OrganizeError("Déplacement entre volumes refusé.")
    if _collision(destination):
        raise OrganizeError("Destination déjà présente : aucun remplacement autorisé.")
    current = _snapshot(source)
    if not plan.fingerprint or _fingerprint(current) != plan.fingerprint:
        raise OrganizeError("Le dossier a changé depuis la prévisualisation : générer un nouveau plan.")
    hardlinked = sum(entry.kind == "file" and entry.nlink > 1 for entry in current)
    if hardlinked and not allow_hardlinks:
        raise OrganizeError("Des liens physiques sont présents : accord distinct obligatoire.")
    _mkdir_safe(folder)
    journal = folder / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex + ".json")
    record = {"version": JOURNAL_VERSION, "state": "intent", "created_at": datetime.now(timezone.utc).isoformat(),
              "source": str(source), "destination": str(destination), "fingerprint": plan.fingerprint,
              "files": [asdict(entry) for entry in current],
              "source_device": plan.source_device, "source_inode": plan.source_inode,
              "hardlinks_acknowledged": allow_hardlinks, "error": ""}
    _save_journal(journal, record)
    try:
        _mkdir_safe(destination.parent)
        # Revalidation après la préparation des dossiers/journal.
        if _fingerprint(_snapshot(source)) != plan.fingerprint:
            raise OrganizeError("Le dossier a changé avant le renommage.")
        _rename_without_replace(source, destination, (plan.source_device, plan.source_inode))
        record["state"] = "applied"
        _save_journal(journal, record)
        return journal
    except (OSError, OrganizeError) as exc:
        record["state"] = "failed"
        record["error"] = str(exc)
        try:
            _save_journal(journal, record)
        except OSError:
            pass  # L'intention durable précédente permet encore la récupération.
        raise OrganizeError(f"Déplacement interrompu : {exc}. Journal : {journal}", journal) from exc


def restore_journal(journal_path: str | Path, confirmed: bool = False) -> Path:
    if not confirmed:
        raise OrganizeError("Une confirmation manuelle est obligatoire pour restaurer.")
    journal = _absolute(journal_path)
    _check_ancestors(journal.parent)
    if _is_link(journal):
        raise OrganizeError("Journal lié symboliquement refusé.")
    try:
        if journal.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("Journal trop volumineux")
        record = json.loads(journal.read_text("utf-8"))
        if (record.get("version") != JOURNAL_VERSION or record.get("state") not in
                {"intent", "applied", "restoring", "restored", "failed"}):
            raise ValueError("Version ou état de journal inconnu")
        source, destination = _absolute(record["source"]), _absolute(record["destination"])
        expected = (int(record["source_device"]), int(record["source_inode"]))
        fingerprint = record["fingerprint"]
        entries = [FileEntry(**entry) for entry in record["files"]]
        if not isinstance(fingerprint, str) or fingerprint != _fingerprint(entries):
            raise ValueError("Empreinte de journal incohérente")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise OrganizeError(f"Journal invalide : {exc}") from exc
    if source == destination or source.is_relative_to(destination) or destination.is_relative_to(source):
        raise OrganizeError("Relation de chemins invalide dans le journal.")
    _journal_folder(journal.parent, source, destination)
    source_exists, destination_exists = source.exists(), destination.exists()
    if source_exists and not destination_exists:
        # Renommage non effectué, ou restauration terminée avant l'écriture de l'état.
        if _fingerprint(_snapshot(source)) != fingerprint:
            raise OrganizeError("La source existe mais a changé : restauration indéterminée.")
        record["state"] = "restored"
        record["restored_at"] = datetime.now(timezone.utc).isoformat()
        _save_journal(journal, record)
        return journal
    if not destination_exists or source_exists or _collision(source):
        raise OrganizeError("Restauration bloquée : source occupée ou destination absente ; aucun remplacement autorisé.")
    _check_ancestors(destination)
    _check_ancestors(source.parent, allow_missing=True)
    if _fingerprint(_snapshot(destination)) != fingerprint:
        raise OrganizeError("Le dossier déplacé a changé depuis l'opération : restauration automatique refusée.")
    info = destination.lstat()
    if (info.st_dev, info.st_ino) != expected:
        raise OrganizeError("Le dossier déplacé a été remplacé : restauration refusée.")
    record["state"] = "restoring"
    _save_journal(journal, record)
    try:
        _mkdir_safe(source.parent)
        if _fingerprint(_snapshot(destination)) != fingerprint:
            raise OrganizeError("Le dossier a changé avant la restauration.")
        _rename_without_replace(destination, source, expected)
        record["state"] = "restored"
        record["restored_at"] = datetime.now(timezone.utc).isoformat()
        _save_journal(journal, record)
        return journal
    except (OSError, OrganizeError) as exc:
        record["error"] = str(exc)
        try:
            _save_journal(journal, record)
        except OSError:
            pass
        raise OrganizeError(f"Restauration interrompue : {exc}. Journal : {journal}", journal) from exc
