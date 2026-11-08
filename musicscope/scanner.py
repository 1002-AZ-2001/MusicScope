"""Parcours conservateur des sources : cache local, inventaire atomique."""
from __future__ import annotations

import os
import errno
import re
import stat as stat_module
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Callable

from .audit import audit_album
from .cache import Cache, ScanCancelled, check_cancel
from .identity import MountCache, file_identity
from .metadata import AUDIO_EXTENSIONS, read_track
from .models import Album, Inventory
from .normalization import name_key

DISC_FOLDER = re.compile(r"^(?:cd|disc|disk|disque)[\s._-]*(\d+)$", re.I)


class ScanUnavailable(OSError):
    """Un parcours incomplet ne doit pas remplacer l'inventaire précédent."""


def is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def scan(root: str | Path, cache: Cache, cancel: Event | None = None,
         progress: Callable[[int, int, str], None] | None = None) -> Inventory:
    root = Path(root).expanduser().absolute()
    check_cancel(cancel)
    cache_location = cache.path.resolve()
    resolved_root = root.resolve()
    if cache_location == resolved_root or resolved_root in cache_location.parents:
        raise ValueError("Le cache doit être situé hors de la médiathèque")
    if is_link(root) or not root.is_dir():
        raise ScanUnavailable(f"Racine inaccessible ou lien ignoré : {root}")
    found: list[tuple[Path, os.stat_result]] = []
    traversal_errors: list[str] = []
    pending = [root]
    while pending:
        check_cancel(cancel)
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    check_cancel(cancel)
                    path = Path(entry.path)
                    if is_link(path):
                        continue
                    try:
                        info = entry.stat(follow_symlinks=False)
                        if stat_module.S_ISDIR(info.st_mode):
                            pending.append(path)
                        elif stat_module.S_ISREG(info.st_mode) and path.suffix.casefold() in AUDIO_EXTENSIONS:
                            found.append((path, info))
                    except OSError as exc:
                        traversal_errors.append(f"{path} : {exc}")
        except OSError as exc:
            traversal_errors.append(f"{directory} : {exc}")
        if progress:
            progress(0, 0, f"Découverte : {directory}")
    if traversal_errors:
        raise ScanUnavailable("Parcours incomplet ; inventaire précédent conservé.\n" + "\n".join(traversal_errors))

    inventory = Inventory(str(root), scanned_at=datetime.now(timezone.utc).isoformat())
    albums: dict[Path, Album] = {}
    grouped_folders: dict[Path, set[Path]] = {}
    mounts = MountCache()
    with cache.deferred_commits():
        unstable_source = _read_tracks(root, found, cache, mounts, inventory, albums,
                                       grouped_folders, cancel, progress)
    for folder, album in sorted(albums.items()):
        if folder in grouped_folders[folder] and len(grouped_folders[folder]) > 1:
            album.issues.append("Regroupement ambigu : pistes à la racine de l'album et dans des sous-dossiers de disques")
        titles = {name_key(value) for value in album.values("album")}
        artist_signatures = {tuple(sorted(name_key(value) for value in signature))
                             for signature in album.tag_signatures("albumartist")}
        if len(titles) > 1 or len(artist_signatures) > 1:
            album.issues.append("Regroupement ambigu : plusieurs albums ou artistes d'album dans le même dossier")
        album.issues.extend(audit_album(album))
        album.issues = list(dict.fromkeys(album.issues))
        inventory.albums.append(album)
    check_cancel(cancel)
    if unstable_source:
        raise ScanUnavailable("Source inaccessible ou modifiée pendant l'analyse ; inventaire précédent conservé.\n"
                              + "\n".join(inventory.errors))
    # A vanished/unmounted tree can appear empty rather than raise an I/O error.
    previous = cache.load_inventory(root)
    if not found and previous and previous.albums:
        raise ScanUnavailable("La racine est vide alors qu'un inventaire existe ; vérifier le montage. Inventaire précédent conservé.")
    cache.save_inventory(inventory)
    return inventory


def _read_tracks(root: Path, found: list[tuple[Path, os.stat_result]], cache: Cache,
                 mounts: MountCache, inventory: Inventory, albums: dict[Path, Album],
                 grouped_folders: dict[Path, set[Path]], cancel: Event | None,
                 progress: Callable[[int, int, str], None] | None) -> bool:
    """Lit ou réutilise les pistes ; renvoie vrai si la source a été instable."""
    unstable_source = False
    for index, (path, initial) in enumerate(sorted(found), start=1):
        check_cancel(cancel)
        folder = path.parent
        disc_match = DISC_FOLDER.fullmatch(folder.name) if folder != root else None
        album_folder = folder.parent if disc_match else folder
        album = albums.setdefault(album_folder, Album(str(album_folder)))
        grouped_folders.setdefault(album_folder, set()).add(folder)
        try:
            if is_link(path):
                raise OSError("Le fichier est devenu un lien pendant le scan")
            current = path.stat(follow_symlinks=False)
            if (current.st_size, current.st_mtime_ns) != (initial.st_size, initial.st_mtime_ns):
                raise OSError("Le fichier a changé pendant le scan")
            track = cache.get_track(path, current)
            if track is None:
                track = read_track(path, current)
                final = path.stat(follow_symlinks=False)
                if (final.st_size, final.st_mtime_ns, final.st_ino) != (current.st_size, current.st_mtime_ns, current.st_ino):
                    raise OSError("Le fichier a changé pendant la lecture des tags")
                cache.put_track(track)
            track.device, track.inode = file_identity(path, current, mounts)
            if disc_match:
                inferred = int(disc_match.group(1))
                if not track.value("discnumber"):
                    track.tags["discnumber"] = [str(inferred)]
                elif track.disc != inferred:
                    album.issues.append(f"Regroupement ambigu : {folder.name} contredit le numéro de disque de {path.name}")
            album.tracks.append(track)
        except (OSError, ValueError, TypeError) as exc:
            if isinstance(exc, OSError) and exc.errno not in (errno.EACCES, errno.EPERM):
                # Disparition, déconnexion ou modification concurrente : ne pas
                # transformer un scan instable en inventaire de référence.
                unstable_source = True
            album.complete = False
            issue = f"Fichier illisible : {path} : {exc}"
            album.issues.append(issue)
            inventory.errors.append(issue)
        if progress:
            progress(index, len(found), str(path))
    return unstable_source
