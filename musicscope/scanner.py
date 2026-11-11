"""Parcours conservateur des sources : cache local, inventaire atomique."""
from __future__ import annotations

import os
import errno
import re
import stat as stat_module
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Callable

from .audit import audit_album
from .cache import Cache, ScanCancelled, check_cancel
from .identity import MountCache, file_identity, is_local_storage
from .metadata import AUDIO_EXTENSIONS, read_track
from .models import Album, Inventory, Track
from .normalization import name_key

QUARANTINE_DIR = "_MusicScope_doublons"  # même valeur que organizer.QUARANTINE_DIR

DISC_FOLDER = re.compile(r"^(?:cd|disc|disk|disque)[\s._-]*(\d+)$", re.I)
# Sur un partage réseau, les accès (stat, lecture des tags) attendent surtout
# la latence : plusieurs requêtes en vol la masquent. Sur un disque local, le
# décodage des tags est limité par le GIL et des threads ralentissent le scan.
NETWORK_WORKERS = 8


def auto_workers(root: Path, mounts: MountCache | None = None) -> int:
    """Lecture séquentielle sur stockage local, parallèle sur partage réseau ou inconnu."""
    return 1 if is_local_storage(root, mounts) else NETWORK_WORKERS


class ScanUnavailable(OSError):
    """Un parcours incomplet ne doit pas remplacer l'inventaire précédent."""


def is_link(path: Path) -> bool:
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def scan(root: str | Path, cache: Cache, cancel: Event | None = None,
         progress: Callable[[int, int, str], None] | None = None,
         workers: int | None = None) -> Inventory:
    """Inventaire en lecture seule ; `workers` threads pour les accès disque.

    Sans `workers`, le choix est automatique : un thread sur stockage local,
    plusieurs sur un partage réseau (voir `auto_workers`).

    Le cache SQLite, la table des montages et l'assemblage de l'inventaire
    restent dans le thread appelant : le résultat ne dépend pas de l'ordre
    d'achèvement des lectures parallèles.
    """
    if workers is not None and workers < 1:
        raise ValueError("Au moins un thread de lecture est nécessaire")
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
                    if is_link(path) or (directory == root and entry.name == QUARANTINE_DIR):
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
    if workers is None:
        workers = auto_workers(root, mounts)
    with cache.deferred_commits():
        unstable_source = _read_tracks(root, found, cache, mounts, inventory, albums,
                                       grouped_folders, cancel, progress, workers)
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
    # Parcours complet et stable : les entrées des fichiers disparus sont inutiles.
    cache.prune_missing(root, {str(path) for path, _ in found})
    return inventory


def _probe(path: Path, initial: os.stat_result, cancel: Event | None) -> os.stat_result:
    """Thread de lecture : le fichier doit être resté identique depuis la découverte."""
    check_cancel(cancel)
    if is_link(path):
        raise OSError("Le fichier est devenu un lien pendant le scan")
    current = path.stat(follow_symlinks=False)
    if (current.st_size, current.st_mtime_ns) != (initial.st_size, initial.st_mtime_ns):
        raise OSError("Le fichier a changé pendant le scan")
    return current


def _read(path: Path, current: os.stat_result, cancel: Event | None) -> Track:
    """Thread de lecture : tags lus puis version du fichier revérifiée."""
    check_cancel(cancel)
    track = read_track(path, current)
    final = path.stat(follow_symlinks=False)
    if (final.st_size, final.st_mtime_ns, final.st_ino) != (current.st_size, current.st_mtime_ns, current.st_ino):
        raise OSError("Le fichier a changé pendant la lecture des tags")
    return track


def _collect(pool: ThreadPoolExecutor | None, function: Callable, jobs: list[tuple[int, tuple]],
             cancel: Event | None, on_result: Callable[[int, object], None],
             on_error: Callable[[int, Exception], None]) -> None:
    """Exécute `function` pour chaque tâche ; résultats traités dans le thread appelant.

    Sans pool, les fichiers sont traités un par un, avec progression et
    annulation à chaque fichier. Avec un pool, après une annulation, les
    lectures non commencées sont abandonnées mais celles déjà terminées sont
    encore transmises : leurs tags restent en cache pour le prochain scan.
    """
    def deliver(index: int, call: Callable[[], object]) -> bool:
        try:
            value = call()
        except (ScanCancelled, CancelledError):
            return False
        except (OSError, ValueError, TypeError) as exc:
            on_error(index, exc)
        else:
            on_result(index, value)
        return True

    if pool is None:
        for index, args in jobs:
            check_cancel(cancel)
            if not deliver(index, lambda: function(*args)):
                break
        check_cancel(cancel)
        return
    futures = {pool.submit(function, *args): index for index, args in jobs}
    cancelled = False
    for future in as_completed(futures):
        if not deliver(futures[future], future.result):
            cancelled = True
            continue
        if not cancelled and cancel is not None and cancel.is_set():
            cancelled = True
            for pending in futures:
                pending.cancel()
    if cancelled:
        raise ScanCancelled("Analyse annulée")
    check_cancel(cancel)


def _read_tracks(root: Path, found: list[tuple[Path, os.stat_result]], cache: Cache,
                 mounts: MountCache, inventory: Inventory, albums: dict[Path, Album],
                 grouped_folders: dict[Path, set[Path]], cancel: Event | None,
                 progress: Callable[[int, int, str], None] | None, workers: int) -> bool:
    """Lit ou réutilise les pistes ; renvoie vrai si la source a été instable."""
    ordered = sorted(found)
    total = len(ordered)
    currents: list[os.stat_result | None] = [None] * total
    outcomes: list[Track | Exception | None] = [None] * total
    done = 0

    def failed(index: int, exc: Exception) -> None:
        nonlocal done
        outcomes[index] = exc
        done += 1
        if progress:
            progress(done, total, str(ordered[index][0]))

    def probed(index: int, current: os.stat_result) -> None:
        nonlocal done
        currents[index] = current
        done += 1
        if progress:
            progress(done, total, f"Vérification : {ordered[index][0]}")

    def read(index: int, track: Track) -> None:
        nonlocal done
        cache.put_track(track)
        outcomes[index] = track
        done += 1
        if progress:
            progress(done, len(misses), str(ordered[index][0]))

    pool = (None if workers == 1
            else ThreadPoolExecutor(max_workers=workers, thread_name_prefix="musicscope-scan"))
    try:
        _collect(pool, _probe, [(index, (path, initial, cancel)) for index, (path, initial) in enumerate(ordered)],
                 cancel, probed, failed)
        misses = []
        for index, (path, _) in enumerate(ordered):
            check_cancel(cancel)
            current = currents[index]
            if current is None:
                continue
            track = cache.get_track(path, current)
            if track is None:
                misses.append(index)
            else:
                outcomes[index] = track
        done = 0
        _collect(pool, _read, [(index, (ordered[index][0], currents[index], cancel)) for index in misses],
                 cancel, read, failed)
    finally:
        if pool is not None:
            # Annulation ou erreur : les lectures non commencées sont abandonnées.
            pool.shutdown(wait=True, cancel_futures=True)

    unstable_source = False
    for index, (path, _) in enumerate(ordered):
        check_cancel(cancel)
        folder = path.parent
        disc_match = DISC_FOLDER.fullmatch(folder.name) if folder != root else None
        album_folder = folder.parent if disc_match else folder
        album = albums.setdefault(album_folder, Album(str(album_folder)))
        grouped_folders.setdefault(album_folder, set()).add(folder)
        try:
            outcome = outcomes[index]
            if isinstance(outcome, Exception):
                raise outcome
            assert outcome is not None
            track = outcome
            track.device, track.inode = file_identity(path, currents[index], mounts)
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
    return unstable_source
