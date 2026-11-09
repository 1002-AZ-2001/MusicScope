"""Cache des points de montage : mêmes décisions que la sonde unitaire, moins de lectures."""
from __future__ import annotations

import io
import os
import random
import sqlite3
from pathlib import Path
from threading import Event

import pytest

from musicscope.cache import Cache, ScanCancelled
from musicscope.identity import (
    DRIVE_FIXED, FILE_SUPPORTS_HARD_LINKS, LOCAL_POSIX_FILESYSTEMS, MountCache, WindowsVolume,
    file_identity, read_mount_points, windows_file_identity,
)
from musicscope.models import Track
from musicscope.scanner import scan

posix_only = pytest.mark.skipif(os.name != "posix", reason="La table mountinfo concerne Linux.")


def stat_identity(device=42, inode=8):
    return os.stat_result((0o100644, inode, device, 1, 0, 0, 1, 0, 0, 0))


def reference_is_local(lines: list[str], path: Path) -> bool:
    """Algorithme de la version 0.2.1, conservé ici comme oracle."""
    import re
    mounts = []
    for line in lines:
        left, right = line.split(" - ", 1)
        mount = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), left.split()[4])
        if path == Path(mount) or Path(mount) in path.parents:
            mounts.append((len(mount), right.split()[0]))
    longest = max(length for length, _ in mounts) if mounts else 0
    filesystems = {filesystem for length, filesystem in mounts if length == longest}
    return len(filesystems) == 1 and filesystems <= LOCAL_POSIX_FILESYSTEMS


def mountinfo(lines: list[str]):
    return lambda *args, **kwargs: io.StringIO("".join(line + "\n" for line in lines))


def line(mount: str, filesystem: str, number: int = 1) -> str:
    return f"{number} 0 0:{number} / {mount} rw - {filesystem} source rw"


@posix_only
def test_decisions_match_reference_on_random_tables(monkeypatch):
    rng = random.Random(20261108)
    points = ["/", "/music", "/music/nas", "/music/nas/Artiste", "/mnt", "/mnt/usb", "/music/local\\040disk"]
    types = ["ext4", "zfs", "nfs", "cifs", "btrfs", "fuse.sshfs", "overlay"]
    paths = [Path(p) for p in ("/music/a/01.flac", "/music/nas/Artiste/Album/01.flac", "/music/nas/x.mp3",
                               "/mnt/usb/Album/CD1/01.flac", "/srv/01.flac", "/music/nas/Artiste",
                               "/music/local disk/Album/01.flac")]
    for _ in range(300):
        lines = [line(point, rng.choice(types), index)
                 for index, point in enumerate(rng.sample(points, rng.randint(1, len(points))), 1)]
        if rng.random() < 0.3:
            # Montage empilé au même point, éventuellement d'un autre type.
            lines.append(line(rng.choice(points), rng.choice(types), 99))
        monkeypatch.setattr("builtins.open", mountinfo(lines))
        cache = MountCache()
        for path in paths:
            assert cache.is_local(path, 42) == reference_is_local(lines, path), (lines, path)


@posix_only
def test_file_bind_mount_is_not_hidden_by_folder_memo(monkeypatch):
    lines = [line("/", "ext4"), line("/music/album/02.flac", "nfs", 2)]
    monkeypatch.setattr("builtins.open", mountinfo(lines))
    cache = MountCache()
    assert cache.is_local(Path("/music/album/01.flac"), 42)
    assert not cache.is_local(Path("/music/album/02.flac"), 42)
    assert file_identity(Path("/music/album/02.flac"), stat_identity(), cache) == (None, None)


def test_table_read_once_per_device_and_reloaded_for_new_device():
    tables = [{"/": frozenset({"ext4"})}, {"/": frozenset({"ext4"}), "/music": frozenset({"nfs"})}]
    reads = []

    def reader():
        reads.append(1)
        return tables[min(len(reads), len(tables)) - 1]

    cache = MountCache(reader)
    for number in range(500):
        assert cache.is_local(Path(f"/music/Album {number % 20}/{number:03d}.flac"), 42)
    assert len(reads) == 1
    # Un NAS monté pendant le scan apparaît avec un autre st_dev : relecture.
    assert not cache.is_local(Path("/music/Album 0/001.flac"), 77)
    assert len(reads) == cache.reloads == 2


def test_unreadable_table_is_conservative_and_retried_on_new_device():
    calls = []

    def reader():
        calls.append(1)
        if len(calls) == 1:
            raise OSError("mountinfo indisponible")
        return {"/": frozenset({"ext4"})}

    cache = MountCache(reader)
    assert not cache.is_local(Path("/music/01.flac"), 1)
    assert not cache.is_local(Path("/music/02.flac"), 1)
    assert cache.is_local(Path("/music/03.flac"), 2)


@posix_only
def test_read_mount_points_groups_stacked_mounts(monkeypatch):
    monkeypatch.setattr("builtins.open", mountinfo([
        line("/", "ext4"), line("/music", "ext4", 2), line("/music", "nfs", 3), line("/a\\040b", "zfs", 4)]))
    assert read_mount_points() == {"/": frozenset({"ext4"}), "/music": frozenset({"ext4", "nfs"}),
                                   "/a b": frozenset({"zfs"})}


@posix_only
def test_scan_reads_mount_table_once(tmp_path, monkeypatch):
    source = tmp_path / "library"
    for album in range(6):
        for number in range(1, 5):
            path = source / f"Album {album}" / f"{number:02d}.flac"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"audio %d %d" % (album, number))
    monkeypatch.setattr("musicscope.scanner.read_track", lambda path, stat: Track(
        str(path), "FLAC", stat.st_size, stat.st_mtime_ns, 1.0, {"album": [path.parent.name]}))
    reads = []
    original = read_mount_points

    def counted():
        reads.append(1)
        return original()

    monkeypatch.setattr("musicscope.identity.read_mount_points", counted)
    with Cache(tmp_path / "cache" / "inventory.sqlite3") as cache:
        inventory = scan(source, cache)
    assert sum(len(album.tracks) for album in inventory.albums) == 24
    assert len(reads) == 1


def test_windows_volume_probe_memoized_per_folder_and_serial():
    probes = []
    volume = WindowsVolume(DRIVE_FIXED, "NTFS", 42, FILE_SUPPORTS_HARD_LINKS)

    def probe(path):
        probes.append(path)
        return volume

    cache = MountCache(lambda: {})
    for name in ("01.flac", "02.flac", "03.flac"):
        assert windows_file_identity(rf"C:\Musique\Album\{name}", stat_identity(42), lambda text: cache.windows_volume(
            text, 42, probe)) == (42, 8)
    assert len(probes) == 1
    cache.windows_volume(r"C:\Musique\Autre\01.flac", 42, probe)
    cache.windows_volume(r"C:\Musique\Album\01.flac", 43, probe)
    assert len(probes) == 3


def test_windows_probe_error_is_not_memoized():
    calls = []

    def flaky(path):
        calls.append(path)
        if len(calls) == 1:
            raise OSError("volume occupé")
        return WindowsVolume(DRIVE_FIXED, "NTFS", 42, FILE_SUPPORTS_HARD_LINKS)

    cache = MountCache(lambda: {})
    first = windows_file_identity(r"C:\M\A\01.flac", stat_identity(42), lambda t: cache.windows_volume(t, 42, flaky))
    second = windows_file_identity(r"C:\M\A\02.flac", stat_identity(42), lambda t: cache.windows_volume(t, 42, flaky))
    assert (first, second) == ((None, None), (42, 8))


def test_deferred_commits_batch_and_survive_cancellation(tmp_path, monkeypatch):
    database = tmp_path / "cache" / "inventory.sqlite3"
    source = tmp_path / "library"
    paths = []
    for number in range(1, 11):
        path = source / "Album" / f"{number:02d}.flac"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"audio %d" % number)
        paths.append(path)
    cancel = Event()
    read = []

    def reader(path, stat):
        read.append(path)
        if len(read) == 7:
            cancel.set()
        return Track(str(path), "FLAC", stat.st_size, stat.st_mtime_ns, 1.0, {"album": ["Album"]})

    monkeypatch.setattr("musicscope.scanner.read_track", reader)
    with Cache(database) as cache:
        with pytest.raises(ScanCancelled):
            scan(source, cache, cancel, workers=1)
        assert cache.load_inventory(source) is None
    with sqlite3.connect(database) as other:
        assert other.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 7
    read.clear()
    with Cache(database) as cache:
        scan(source, cache)
    assert len(read) == 3


def test_deferred_commits_are_invisible_to_other_connections_until_batch(tmp_path):
    database = tmp_path / "inventory.sqlite3"
    with Cache(database) as cache, sqlite3.connect(database) as other:
        count = lambda: other.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        with cache.deferred_commits(every=3):
            for number in range(5):
                cache.put_track(Track(f"/m/{number}.flac", size=number, mtime_ns=number))
            assert count() == 3
        assert count() == 5
        cache.put_track(Track("/m/direct.flac"))
        assert count() == 6


def test_benchmark_script_reports_scans(tmp_path, capsys):
    import importlib.util
    import json
    spec = importlib.util.spec_from_file_location(
        "benchmark_scan", Path(__file__).parents[1] / "scripts" / "benchmark_scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report_path = tmp_path / "bench.json"
    assert module.main(["--albums", "3", "--tracks", "2", "--duplicate-every", "2",
                        "--json", str(report_path)]) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["library"]["files"] == 10
    assert report["cold_scan"]["tracks"] == report["warm_scan"]["tracks"] == 10
    assert report["cold_scan"]["errors"] == 0
    assert report["comparison"]["pairs"] == 2
    if os.name == "posix":
        assert report["cold_scan"]["mountinfo_reads"] == 1


def test_benchmark_script_refuses_non_empty_library(tmp_path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "benchmark_scan", Path(__file__).parents[1] / "scripts" / "benchmark_scan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    library = tmp_path / "personal"
    library.mkdir()
    (library / "keep.flac").write_bytes(b"precious")
    with pytest.raises(SystemExit):
        module.main(["--library", str(library), "--albums", "1"])
    assert (library / "keep.flac").read_bytes() == b"precious"
