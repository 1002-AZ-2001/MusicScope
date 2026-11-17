"""Parcours conservateur des sources : cache local, inventaire atomique."""
from __future__ import annotations

import os
import errno
import re
import stat as stat_module
import time
from concurrent.futures import FIRST_COMPLETED, CancelledError, ThreadPoolExecutor, wait
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
    progress = _throttled(progress)
    mounts = MountCache(root=root)
    if workers is None:
        workers = auto_workers(root, mounts)
    pool = (None if workers == 1
            else ThreadPoolExecutor(max_workers=workers, thread_name_prefix="musicscope-scan"))
    inventory = Inventory(str(root), scanned_at=datetime.now(timezone.utc).isoformat())
    albums: dict[Path, Album] = {}
    grouped_folders: dict[Path, set[Path]] = {}
    try:
        found, traversal_errors, skipped = _discover(root, pool, cancel, progress)
        if traversal_errors:
            raise ScanUnavailable("Parcours incomplet ; inventaire précédent conservé.\n" + "\n".join(traversal_errors))
        inventory.errors.extend(skipped)
        with cache.deferred_commits():
            unstable_source = _read_tracks(root, found, cache, mounts, inventory, albums,
                                           grouped_folders, cancel, progress, pool)
    finally:
        if pool is not None:
            # Annulation ou erreur : les lectures non commencées sont abandonnées.
            pool.shutdown(wait=True, cancel_futures=True)
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
    previous = cache.load_inventory(root) if not found else None
    if not found and previous and previous.albums:
        raise ScanUnavailable("La racine est vide alors qu'un inventaire existe ; vérifier le montage. Inventaire précédent conservé.")
    cache.save_inventory(inventory)
    # Parcours complet et stable : les entrées des fichiers disparus sont inutiles. Un dossier
    # illisible a pu masquer des fichiers encore présents : rien n'est purgé dans ce cas.
    if not skipped:
        cache.prune_missing(root, {str(path) for path, _ in found})
    return inventory


def _throttled(progress: Callable[[int, int, str], None] | None, interval: float = 0.1):
    """Au plus une notification toutes les 100 ms, plus la dernière de chaque phase : une par
    fichier inondait la file d'événements Qt et disputait le GIL aux threads de lecture."""
    if progress is None:
        return None
    last = [0.0]

    def notify(current: int, total: int, text: str) -> None:
        now = time.monotonic()
        if now - last[0] >= interval or (total and current >= total):
            last[0] = now
            progress(current, total, text)
    return notify


def _scan_directory(directory: Path, is_root: bool):
    """Un dossier : sous-dossiers, fichiers audio (un seul lstat chacun), erreurs.

    Le type des entrées vient de readdir (d_type) : ni les dossiers, ni les fichiers non
    audio (pochettes, .nfo, .part) ne coûtent d'appel stat, précieux sur un partage réseau."""
    folders, files, errors, skipped = [], [], [], []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.is_symlink() or (hasattr(entry, "is_junction") and entry.is_junction()):
                        continue
                    if is_root and entry.name == QUARANTINE_DIR:
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        folders.append(Path(entry.path))
                        continue
                    if os.path.splitext(entry.name)[1].casefold() not in AUDIO_EXTENSIONS:
                        continue
                    info = entry.stat(follow_symlinks=False)
                    if stat_module.S_ISREG(info.st_mode):
                        files.append((Path(entry.path), info))
                except FileNotFoundError:
                    continue  # disparu entre la lecture du dossier et son examen (fichier temporaire)
                except OSError as exc:
                    errors.append(f"{entry.path} : {exc}")
    except PermissionError as exc:
        if is_root:
            errors.append(f"{directory} : {exc}")
        else:
            # Dossier interdit (ex. @eaDir d'un NAS) : signalé, sans perdre le reste du scan.
            skipped.append(f"Dossier illisible ignoré : {directory} : {exc}")
    except FileNotFoundError:
        if is_root:
            errors.append(f"{directory} : racine disparue")
    except OSError as exc:
        errors.append(f"{directory} : {exc}")
    return folders, files, errors, skipped


def _discover(root: Path, pool: ThreadPoolExecutor | None, cancel: Event | None,
              progress: Callable[[int, int, str], None] | None):
    """Parcours niveau par niveau ; sur un partage réseau, les dossiers d'un même niveau sont
    lus en parallèle (la latence de chaque readdir se recouvre)."""
    found: list[tuple[Path, os.stat_result]] = []
    errors: list[str] = []
    skipped: list[str] = []
    level = [root]
    seen = 0
    while level:
        check_cancel(cancel)
        if pool is None:
            results = []
            for directory in level:
                check_cancel(cancel)
                results.append(_scan_directory(directory, directory == root))
                seen += 1
                if progress:
                    progress(0, 0, f"Découverte : {directory}")
        else:
            results = []
            for result in pool.map(lambda directory: _scan_directory(directory, directory == root), level):
                check_cancel(cancel)  # les lectures restantes sont abandonnées à la fermeture du pool
                results.append(result)
                seen += 1
                if progress:
                    progress(0, 0, f"Découverte : {seen} dossiers")
        level = []
        for folders, files, folder_errors, folder_skipped in results:
            level.extend(folders)
            found.extend(files)
            errors.extend(folder_errors)
            skipped.extend(folder_skipped)
    return found, errors, skipped


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
    # Fenêtre glissante : quelques tâches en vol par thread, au lieu d'un objet Future par
    # fichier créé d'avance (≈ 190 Mio pour 96 585 fichiers) ; l'annulation est immédiate.
    window = max(1, pool._max_workers * 4)
    queue = iter(jobs)
    running: dict = {}
    cancelled = False

    def refill() -> None:
        while len(running) < window and not cancelled:
            try:
                index, args = next(queue)
            except StopIteration:
                return
            running[pool.submit(function, *args)] = index

    refill()
    while running:
        finished, _ = wait(running, return_when=FIRST_COMPLETED)
        for future in finished:
            index = running.pop(future)
            if not deliver(index, future.result):
                cancelled = True
        if not cancelled and cancel is not None and cancel.is_set():
            cancelled = True
        if cancelled:
            for future in running:
                future.cancel()
        refill()
    if cancelled:
        raise ScanCancelled("Analyse annulée")
    check_cancel(cancel)


def _read_tracks(root: Path, found: list[tuple[Path, os.stat_result]], cache: Cache,
                 mounts: MountCache, inventory: Inventory, albums: dict[Path, Album],
                 grouped_folders: dict[Path, set[Path]], cancel: Event | None,
                 progress: Callable[[int, int, str], None] | None, pool: ThreadPoolExecutor | None) -> bool:
    """Lit ou réutilise les pistes ; renvoie vrai si la source a été instable.

    L'état relevé à la découverte suffit pour interroger le cache ; seules les pistes
    absentes du cache sont lues, puis revérifiées après lecture (voir `_read`)."""
    ordered = sorted(found, key=lambda item: item[0])
    currents: list[os.stat_result] = [info for _, info in ordered]
    outcomes: list[Track | Exception | None] = [None] * len(ordered)
    misses: list[int] = []
    done = 0

    def failed(index: int, exc: Exception) -> None:
        nonlocal done
        outcomes[index] = exc
        done += 1
        if progress:
            progress(done, len(misses), str(ordered[index][0]))

    def read(index: int, track: Track) -> None:
        nonlocal done
        cache.put_track(track, currents[index].st_ctime_ns)
        outcomes[index] = track
        done += 1
        if progress:
            progress(done, len(misses), str(ordered[index][0]))

    for index, (path, current) in enumerate(ordered):
        check_cancel(cancel)
        if progress:
            progress(index + 1, len(ordered), f"Cache : {path}")
        try:
            track = cache.get_track(path, current)
        except UnicodeEncodeError:
            # Nom de fichier non UTF-8 (octets invalides) : illisible pour le cache, signalé seul.
            outcomes[index] = ValueError("Nom de fichier non UTF-8 : renommez-le pour l’analyser")
            continue
        if track is None:
            misses.append(index)
        else:
            outcomes[index] = track
    _collect(pool, _read, [(index, (ordered[index][0], currents[index], cancel)) for index in misses],
             cancel, read, failed)

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
