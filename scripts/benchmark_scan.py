"""Test de charge du scan sur une bibliothèque synthétique.

Le script crée lui-même ses fichiers dans un dossier vide ou temporaire ; il
n'ouvre jamais une médiathèque existante. Le cache est placé hors de la
bibliothèque générée. Exemple :

    python scripts/benchmark_scan.py --albums 1000 --tracks 12 --json bench.json

`--latency 1` simule un partage réseau (1 ms par stat, 3 ms par lecture de dossier) :
sans elle, un disque local masque le coût des accès sur un NAS SMB.
"""
from __future__ import annotations

import argparse
import builtins
import json
import os
import platform
import threading
import resource
import shutil
import sys
import tempfile
import time
from contextlib import contextmanager
from importlib.resources import files
from pathlib import Path
from shutil import copyfile

from mutagen.flac import FLAC

from musicscope.cache import Cache
from musicscope.comparison import candidates, compare_albums
from musicscope.scanner import auto_workers, scan

MOUNTINFO = "/proc/self/mountinfo"


def generate(destination: Path, albums: int, tracks: int, duplicate_every: int) -> int:
    """Crée `albums` albums FLAC étiquetés ; une copie octet pour octet tous les N albums."""
    destination = destination.expanduser().absolute()
    if destination.exists() and any(destination.iterdir()):
        raise SystemExit(f"Le dossier de génération doit être vide ou inexistant : {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    templates = [files("musicscope").joinpath(f"data/tone-{tone}.flac") for tone in ("a", "b")]
    created = 0
    for album_index in range(albums):
        artist = f"Artiste {album_index // 10:04d}"
        title = f"Album {album_index:05d}"
        folder = destination / artist / title
        folder.mkdir(parents=True, exist_ok=True)
        for number in range(1, tracks + 1):
            path = folder / f"{number:02d} - Piste {number}.flac"
            copyfile(str(templates[number % 2]), path)
            audio = FLAC(path)
            audio.update({"album": title, "albumartist": artist, "artist": artist,
                          "title": f"Piste {number}", "tracknumber": f"{number}/{tracks}",
                          "discnumber": "1/1", "date": "2024"})
            audio.save()
            created += 1
        if duplicate_every and album_index % duplicate_every == 0:
            shutil.copytree(folder, destination / artist / f"{title} (copie)")
            created += tracks
    return created


class _SlowEntry:
    """DirEntry dont stat() coûte un aller-retour réseau ; le type (d_type) reste gratuit."""
    __slots__ = ("_entry", "_delay")

    def __init__(self, entry, delay):
        self._entry, self._delay = entry, delay

    def __getattr__(self, name):
        return getattr(self._entry, name)

    def __fspath__(self):
        return self._entry.path

    def stat(self, *, follow_symlinks=True):
        time.sleep(self._delay)
        return self._entry.stat(follow_symlinks=follow_symlinks)


@contextmanager
def simulated_latency(root: Path, stat_ms: float):
    """Latence d'un partage réseau sous `root` : stat_ms par stat, trois fois plus par dossier lu.
    Le cache d'attributs du client SMB n'est pas modélisé : c'est un majorant."""
    if not stat_ms:
        yield {}
        return
    prefix, delay = str(root), stat_ms / 1000
    counts, lock = {"stat": 0, "scandir": 0}, threading.Lock()
    original_stat, original_lstat, original_scandir = os.stat, os.lstat, os.scandir

    def inside(path):
        return not isinstance(path, int) and os.fspath(path).startswith(prefix)

    def bump(kind):
        with lock:
            counts[kind] += 1

    def slow_stat(path, *args, **kwargs):
        if inside(path):
            bump("stat")
            time.sleep(delay)
        return original_stat(path, *args, **kwargs)

    def slow_lstat(path, *args, **kwargs):
        if inside(path):
            bump("stat")
            time.sleep(delay)
        return original_lstat(path, *args, **kwargs)

    class SlowScandir:
        def __init__(self, path):
            self._iterator = original_scandir(path)

        def __iter__(self):
            for entry in self._iterator:
                yield _SlowEntry(entry, delay)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self._iterator.close()

        def close(self):
            self._iterator.close()

    def slow_scandir(path="."):
        if inside(path):
            bump("scandir")
            time.sleep(3 * delay)
            return SlowScandir(path)
        return original_scandir(path)

    os.stat, os.lstat, os.scandir = slow_stat, slow_lstat, slow_scandir
    try:
        yield counts
    finally:
        os.stat, os.lstat, os.scandir = original_stat, original_lstat, original_scandir


@contextmanager
def count_mountinfo_reads():
    counter = {"reads": 0}
    original = builtins.open

    def counting(file, *args, **kwargs):
        if str(file) == MOUNTINFO:
            counter["reads"] += 1
        return original(file, *args, **kwargs)

    builtins.open = counting
    try:
        yield counter
    finally:
        builtins.open = original


def timed_scan(root: Path, cache_path: Path, workers: int | None = None, latency_ms: float = 0) -> dict:
    with Cache(cache_path) as cache, count_mountinfo_reads() as counter, \
            simulated_latency(root, latency_ms) as calls:
        start = time.perf_counter()
        inventory = scan(root, cache, workers=workers)
        elapsed = time.perf_counter() - start
    tracks = sum(len(album.tracks) for album in inventory.albums)
    return {"seconds": round(elapsed, 3), "albums": len(inventory.albums), "tracks": tracks,
            "tracks_per_second": round(tracks / elapsed, 1) if elapsed else None,
            "mountinfo_reads": counter["reads"], "errors": len(inventory.errors), "filesystem_calls": calls,
            "inventory": inventory}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--albums", type=int, default=500)
    parser.add_argument("--tracks", type=int, default=12, help="Pistes par album")
    parser.add_argument("--duplicate-every", type=int, default=10,
                        help="Une copie exacte tous les N albums (0 : aucune)")
    parser.add_argument("--library", type=Path, help="Dossier vide où générer (défaut : temporaire)")
    parser.add_argument("--workers", type=int,
                        help="Threads de lecture (défaut : automatique selon le stockage)")
    parser.add_argument("--latency", type=float, default=0, metavar="MS",
                        help="Latence simulée par stat, en millisecondes (ex. 1 pour un NAS SMB)")
    parser.add_argument("--covers", action="store_true",
                        help="Ajouter une pochette cover.jpg et un fichier .nfo par album (fichiers non audio)")
    parser.add_argument("--keep", action="store_true", help="Conserver bibliothèque et cache")
    parser.add_argument("--json", type=Path, help="Écrire le rapport JSON dans ce fichier")
    args = parser.parse_args(argv)

    if args.library:
        args.library = args.library.expanduser().absolute()
        if args.library.exists() and any(args.library.iterdir()):
            # Vérifié avant toute création : le nettoyage final ne touche
            # jamais un dossier qui contenait déjà des fichiers.
            parser.error(f"--library doit être vide ou inexistant : {args.library}")
    workdir = Path(tempfile.mkdtemp(prefix="musicscope-bench-"))
    library = args.library or workdir / "library"
    cache_path = workdir / "cache" / "inventory.sqlite3"
    try:
        start = time.perf_counter()
        files_created = generate(library, args.albums, args.tracks, args.duplicate_every)
        if args.covers:
            for folder in {path.parent for path in library.rglob("*.flac")}:
                (folder / "cover.jpg").write_bytes(b"\xff\xd8" + bytes(2048))
                (folder / "album.nfo").write_text("<album/>", encoding="utf-8")
        generation = time.perf_counter() - start
        # Avec latence simulée, le choix automatique voit un disque local : 8 threads comme sur un NAS.
        workers = args.workers or (8 if args.latency else None)
        cold = timed_scan(library, cache_path, workers, args.latency)
        warm = timed_scan(library, cache_path, workers, args.latency)
        inventory = warm.pop("inventory")
        cold.pop("inventory")

        start = time.perf_counter()
        pairs = candidates(inventory.albums)
        statuses: dict[str, int] = {}
        for left, right in pairs:
            status = compare_albums(left, right).status
            statuses[status] = statuses.get(status, 0) + 1
        comparison = {"seconds": round(time.perf_counter() - start, 3),
                      "pairs": len(pairs), "statuses": statuses}

        report = {
            "python": platform.python_version(), "platform": platform.platform(),
            "workers": workers or auto_workers(library), "latency_ms": args.latency,
            "library": {"files": files_created, "generation_seconds": round(generation, 2)},
            "cold_scan": cold, "warm_scan": warm, "comparison": comparison,
            "max_rss_mib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
            "cache_mib": round(cache_path.stat().st_size / 2**20, 2),
        }
        text = json.dumps(report, ensure_ascii=False, indent=2)
        print(text)
        if args.json:
            args.json.write_text(text + "\n", encoding="utf-8")
        if args.keep:
            print(f"Conservé : {library} ; cache {cache_path}", file=sys.stderr)
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
            if args.library:
                shutil.rmtree(args.library, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
