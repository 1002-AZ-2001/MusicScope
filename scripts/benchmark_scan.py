"""Test de charge du scan sur une bibliothèque synthétique.

Le script crée lui-même ses fichiers dans un dossier vide ou temporaire ; il
n'ouvre jamais une médiathèque existante. Le cache est placé hors de la
bibliothèque générée. Exemple :

    python scripts/benchmark_scan.py --albums 1000 --tracks 12 --json bench.json
"""
from __future__ import annotations

import argparse
import builtins
import json
import platform
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
from musicscope.scanner import scan

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


def timed_scan(root: Path, cache_path: Path) -> dict:
    with Cache(cache_path) as cache, count_mountinfo_reads() as counter:
        start = time.perf_counter()
        inventory = scan(root, cache)
        elapsed = time.perf_counter() - start
    tracks = sum(len(album.tracks) for album in inventory.albums)
    return {"seconds": round(elapsed, 3), "albums": len(inventory.albums), "tracks": tracks,
            "tracks_per_second": round(tracks / elapsed, 1) if elapsed else None,
            "mountinfo_reads": counter["reads"], "errors": len(inventory.errors),
            "inventory": inventory}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--albums", type=int, default=500)
    parser.add_argument("--tracks", type=int, default=12, help="Pistes par album")
    parser.add_argument("--duplicate-every", type=int, default=10,
                        help="Une copie exacte tous les N albums (0 : aucune)")
    parser.add_argument("--library", type=Path, help="Dossier vide où générer (défaut : temporaire)")
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
        generation = time.perf_counter() - start

        cold = timed_scan(library, cache_path)
        warm = timed_scan(library, cache_path)
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
