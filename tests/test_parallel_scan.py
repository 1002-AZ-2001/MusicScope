"""Scan parallèle : même inventaire qu'en séquentiel, SQLite dans le seul thread appelant."""
from __future__ import annotations

import errno
import sqlite3
import threading
import time
from pathlib import Path
from threading import Event

import pytest

from musicscope.cache import Cache, ScanCancelled
from musicscope.models import Track
from musicscope.scanner import ScanUnavailable, scan


def library(root: Path, albums: int = 6, tracks: int = 5) -> list[Path]:
    paths = []
    for album in range(albums):
        for number in range(1, tracks + 1):
            disc = f"CD{1 + number % 2}" if album == 0 else ""
            path = root / f"Artiste {album % 2}" / f"Album {album}" / disc / f"{number:02d}.flac"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio %d %d" % (album, number))
            paths.append(path)
    return paths


def reader(path, stat):
    album = path.parent.parent.name if path.parent.name.startswith("CD") else path.parent.name
    if path.name == "03.flac" and album == "Album 2":
        raise ValueError("tags illisibles")
    return Track(str(path), "FLAC", stat.st_size, stat.st_mtime_ns, 1.0,
                 {"album": [album], "albumartist": ["A"], "tracknumber": [str(int(path.stem))]})


def without_date(inventory):
    data = inventory.to_dict()
    data.pop("scanned_at")
    return data


def test_parallel_inventory_identical_to_sequential(tmp_path, monkeypatch):
    library(tmp_path / "library")

    def slow_reader(path, stat):
        time.sleep(0.001 * (hash(path.name) % 5))  # ordre d'achèvement mélangé
        return reader(path, stat)

    monkeypatch.setattr("musicscope.scanner.read_track", slow_reader)
    results = []
    for workers in (1, 8):
        with Cache(tmp_path / f"cache{workers}" / "inventory.sqlite3") as cache:
            results.append(without_date(scan(tmp_path / "library", cache, workers=workers)))
    assert results[0] == results[1]
    assert any("tags illisibles" in error for error in results[1]["errors"])


def test_sqlite_used_only_from_calling_thread(tmp_path, monkeypatch):
    library(tmp_path / "library")
    monkeypatch.setattr("musicscope.scanner.read_track", reader)
    caller = threading.get_ident()
    threads = set()
    with Cache(tmp_path / "cache" / "inventory.sqlite3") as cache:
        for name in ("get_track", "put_track"):
            original = getattr(cache, name)

            def recorded(*args, _original=original, **kwargs):
                threads.add(threading.get_ident())
                return _original(*args, **kwargs)

            monkeypatch.setattr(cache, name, recorded)
        scan(tmp_path / "library", cache, workers=8)
    assert threads == {caller}


def test_reads_really_overlap(tmp_path, monkeypatch):
    library(tmp_path / "library", albums=2, tracks=8)
    lock = threading.Lock()
    active, peak = 0, 0

    def waiting_reader(path, stat):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return reader(path, stat)

    monkeypatch.setattr("musicscope.scanner.read_track", waiting_reader)
    with Cache(tmp_path / "cache" / "inventory.sqlite3") as cache:
        scan(tmp_path / "library", cache, workers=4)
    assert 2 <= peak <= 4


def test_parallel_cancellation_keeps_every_completed_read(tmp_path, monkeypatch):
    paths = library(tmp_path / "library")
    cancel = Event()
    read: list[Path] = []
    lock = threading.Lock()

    def cancelling_reader(path, stat):
        with lock:
            read.append(path)
            if len(read) == 7:
                cancel.set()
        return reader(path, stat)

    monkeypatch.setattr("musicscope.scanner.read_track", cancelling_reader)
    database = tmp_path / "cache" / "inventory.sqlite3"
    with Cache(database) as cache:
        with pytest.raises(ScanCancelled):
            scan(tmp_path / "library", cache, cancel, workers=4)
        assert cache.load_inventory(tmp_path / "library") is None
    with sqlite3.connect(database) as other:
        cached = {row[0] for row in other.execute("SELECT path FROM tracks")}
    succeeded = {str(path) for path in read if not (path.name == "03.flac" and "Album 2" in str(path))}
    assert len(read) >= 7 and cached == succeeded
    read.clear()
    with Cache(database) as cache:
        scan(tmp_path / "library", cache, workers=4)
    assert len(read) == len(paths) - len(cached)


def test_parallel_disconnect_preserves_snapshot(tmp_path, monkeypatch):
    library(tmp_path / "library")
    monkeypatch.setattr("musicscope.scanner.read_track", reader)
    with Cache(tmp_path / "cache" / "inventory.sqlite3") as cache:
        previous = without_date(scan(tmp_path / "library", cache))
        next(iter((tmp_path / "library").rglob("01.flac"))).write_bytes(b"new size")

        def disconnected(path, stat):
            raise OSError(errno.EIO, "NAS disconnected")

        monkeypatch.setattr("musicscope.scanner.read_track", disconnected)
        with pytest.raises(ScanUnavailable):
            scan(tmp_path / "library", cache, workers=8)
        assert without_date(cache.load_inventory(tmp_path / "library")) == previous


def test_workers_must_be_positive(tmp_path):
    (tmp_path / "library").mkdir()
    with Cache(tmp_path / "cache" / "inventory.sqlite3") as cache:
        with pytest.raises(ValueError):
            scan(tmp_path / "library", cache, workers=0)


def test_auto_workers_sequential_on_local_and_parallel_on_network(tmp_path, monkeypatch):
    from musicscope import scanner
    from musicscope.identity import is_local_storage
    if is_local_storage(tmp_path):
        assert scanner.auto_workers(tmp_path) == 1
    monkeypatch.setattr("musicscope.scanner.is_local_storage", lambda path, mounts=None: False)
    assert scanner.auto_workers(tmp_path) == scanner.NETWORK_WORKERS
    monkeypatch.setattr("musicscope.scanner.is_local_storage", lambda path, mounts=None: True)
    assert scanner.auto_workers(tmp_path) == 1


def test_network_mount_is_not_local_storage(tmp_path, monkeypatch):
    import io
    import os
    from musicscope.identity import is_local_storage
    if os.name != "posix":
        pytest.skip("Table mountinfo Linux")
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(f"1 0 0:1 / / rw - nfs server:/ rw\n"))
    assert not is_local_storage(tmp_path)
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(f"1 0 0:1 / / rw - ext4 /dev/sda rw\n"))
    assert is_local_storage(tmp_path)
    assert not is_local_storage(tmp_path / "absent")


def test_prune_removes_only_missing_files_under_root(tmp_path, monkeypatch):
    paths = library(tmp_path / "library", albums=2, tracks=3)
    other = tmp_path / "library-bis" / "Album" / "01.flac"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"other")
    monkeypatch.setattr("musicscope.scanner.read_track", reader)
    with Cache(tmp_path / "cache" / "inventory.sqlite3") as cache:
        scan(tmp_path / "library", cache)
        scan(tmp_path / "library-bis", cache)
        cache.hash_track(cache.load_inventory(tmp_path / "library").albums[0].tracks[0])
        assert cache.stats()["tracks"] == 7 and cache.stats()["hashes"] == 1
        removed_path = paths[0]
        removed_path.unlink()
        scan(tmp_path / "library", cache)
        stats = cache.stats()
        remaining = {row[0] for row in cache.connection.execute("SELECT path FROM tracks")}
    assert str(removed_path) not in remaining and str(other) in remaining
    assert stats["tracks"] == 6 and stats["inventories"] == 2 and stats["bytes"] > 0


def test_failed_scan_does_not_prune(tmp_path, monkeypatch):
    paths = library(tmp_path / "library", albums=1, tracks=3)
    monkeypatch.setattr("musicscope.scanner.read_track", reader)
    with Cache(tmp_path / "cache" / "inventory.sqlite3") as cache:
        scan(tmp_path / "library", cache)
        paths[0].unlink()
        cancel = Event()
        cancel.set()
        with pytest.raises(ScanCancelled):
            scan(tmp_path / "library", cache, cancel)
        assert cache.stats()["tracks"] == 3


def test_sequential_progress_is_reported_during_tag_reading(tmp_path, monkeypatch):
    library(tmp_path / "library", albums=1, tracks=4)
    events = []

    def tracing_reader(path, stat):
        events.append(("read", path.name))
        return reader(path, stat)

    monkeypatch.setattr("musicscope.scanner.read_track", tracing_reader)
    with Cache(tmp_path / "cache" / "inventory.sqlite3") as cache:
        scan(tmp_path / "library", cache, workers=1,
             progress=lambda done, total, label: events.append(("progress", done, total)))
    reads = [i for i, event in enumerate(events) if event[0] == "read"]
    # Une progression suit chaque lecture, avant la lecture suivante.
    for current, following in zip(reads, reads[1:]):
        assert any(event[0] == "progress" for event in events[current + 1:following])
