from __future__ import annotations

import hashlib
import os
from pathlib import Path
from threading import Event

import pytest

from musicscope.cache import Cache
from musicscope.models import Track
from musicscope.scanner import ScanCancelled, ScanUnavailable, scan


@pytest.fixture
def cache(tmp_path):
    with Cache(tmp_path / "cache" / "inventory.sqlite3") as instance:
        yield instance


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "library"
    path.mkdir()
    return path


def audio(path: Path, content=b"audio"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def reader(path, stat=None):
    stat = stat or path.stat()
    return Track(str(path), "FLAC", stat.st_size, stat.st_mtime_ns, 180.0,
                 {"albumartist": ["Artist"], "artist": ["Artist"], "album": ["Album"],
                  "title": [path.stem], "tracknumber": [str(int(path.stem[:2]))]})


def test_multidisc_and_folder_identity(source, cache, monkeypatch):
    monkeypatch.setattr("musicscope.scanner.read_track", reader)
    for album in ("Original", "Copy"):
        audio(source / "Artist" / album / "CD1" / "01 - A.flac")
        audio(source / "Artist" / album / "Disc 02" / "01 - B.flac")
    result = scan(source, cache)
    assert len(result.albums) == 2
    assert all([t.disc for t in a.ordered_tracks] == [1, 2] for a in result.albums)
    assert len(cache.load_inventory(source).albums) == 2


def test_cache_reuses_read_and_invalidates_changed_file(source, cache, monkeypatch):
    path = audio(source / "Album" / "01.flac")
    calls = []

    def counted(path, stat):
        calls.append(path)
        return reader(path, stat)

    monkeypatch.setattr("musicscope.scanner.read_track", counted)
    scan(source, cache)
    scan(source, cache)
    assert len(calls) == 1
    path.write_bytes(b"different size")
    scan(source, cache)
    assert len(calls) == 2


def test_unreadable_file_marks_album_incomplete(source, cache, monkeypatch):
    path = audio(source / "Album" / "01.flac")

    def broken(*args):
        raise ValueError("invalid codec header")

    monkeypatch.setattr("musicscope.scanner.read_track", broken)
    result = scan(source, cache)
    assert not result.albums[0].complete
    assert str(path) in result.errors[0]


def test_cancel_and_unavailable_preserve_previous(source, cache, monkeypatch):
    path = audio(source / "Album" / "01.flac")
    monkeypatch.setattr("musicscope.scanner.read_track", reader)
    original = scan(source, cache).to_dict()
    cancel = Event()

    def progress(current, total, path):
        if total:
            cancel.set()

    with pytest.raises(ScanCancelled):
        scan(source, cache, cancel, progress)
    assert cache.load_inventory(source).to_dict() == original
    with pytest.raises(ScanUnavailable):
        scan(source / "missing", cache)
    assert cache.load_inventory(source).to_dict() == original
    path.unlink()
    with pytest.raises(ScanUnavailable, match="vide"):
        scan(source, cache)
    assert cache.load_inventory(source).to_dict() == original


def test_directory_error_preserves_snapshot(source, cache, monkeypatch):
    audio(source / "Album" / "01.flac")
    monkeypatch.setattr("musicscope.scanner.read_track", reader)
    previous = scan(source, cache).to_dict()
    original = os.scandir

    def denied(path):
        if Path(path).name == "Album":
            raise PermissionError("denied")
        return original(path)

    monkeypatch.setattr("musicscope.scanner.os.scandir", denied)
    with pytest.raises(ScanUnavailable, match="denied"):
        scan(source, cache)
    assert cache.load_inventory(source).to_dict() == previous


def test_symlinks_not_followed(source, cache, monkeypatch, tmp_path):
    monkeypatch.setattr("musicscope.scanner.read_track", reader)
    outside = audio(tmp_path / "outside" / "01.flac")
    try:
        (source / "alias").symlink_to(outside.parent, target_is_directory=True)
        (source / "02.flac").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert scan(source, cache).albums == []


def test_ambiguous_grouping(source, cache, monkeypatch):
    monkeypatch.setattr("musicscope.scanner.read_track", reader)
    audio(source / "Album" / "01.flac")
    audio(source / "Album" / "CD1" / "02.flac")
    result = scan(source, cache)
    assert any("Regroupement ambigu" in item for item in result.albums[0].issues)


def test_shared_multi_albumartist_credit_not_ambiguous(source, cache, monkeypatch):
    def multi_credit(path, stat):
        result = reader(path, stat)
        result.tags["albumartist"] = ["A", "B"]
        return result
    monkeypatch.setattr("musicscope.scanner.read_track", multi_credit)
    audio(source / "Album" / "01.flac")
    audio(source / "Album" / "02.flac")
    result = scan(source, cache)
    assert not any("ambigu" in item for item in result.albums[0].issues)


def test_hash_stream_cache_changed_and_cancel(source, cache):
    path = audio(source / "01.flac", b"x" * (1024 * 1024 + 20))
    track = reader(path)
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    assert cache.hash_track(track) == expected
    assert cache.hash_track(track) == expected
    cancel = Event()
    cancel.set()
    with pytest.raises(ScanCancelled):
        cache.hash_track(track, cancel)
    path.write_bytes(b"changed")
    with pytest.raises(OSError, match="modifié"):
        cache.hash_track(track)


def test_hardlink_identity(source, cache, monkeypatch):
    monkeypatch.setattr("musicscope.scanner.read_track", reader)
    path = audio(source / "A" / "01.flac")
    destination = source / "B" / "01.flac"
    destination.parent.mkdir()
    try:
        os.link(path, destination)
    except OSError:
        pytest.skip("hardlinks unavailable")
    tracks = [a.tracks[0] for a in scan(source, cache).albums]
    if tracks[0].inode is None:
        pytest.skip("local file identity unavailable")
    assert (tracks[0].device, tracks[0].inode) == (tracks[1].device, tracks[1].inode)


@pytest.mark.skipif(os.name != "posix", reason="Simulation mountinfo Linux ; cas UNC couverts par tests Win32.")
def test_remote_file_identity_unavailable(monkeypatch, tmp_path):
    import io
    from musicscope.scanner import file_identity
    path = audio(tmp_path / "01.flac")
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO("1 0 1:1 / / rw - nfs server:/music rw\n"))
    assert file_identity(path, path.stat()) == (None, None)


def test_disconnect_during_tag_read_preserves_snapshot(source, cache, monkeypatch):
    import errno
    path = audio(source / "Album" / "01.flac")
    monkeypatch.setattr("musicscope.scanner.read_track", reader)
    previous = scan(source, cache).to_dict()
    path.write_bytes(b"changed size requiring uncached read")

    def disconnected(*args):
        raise OSError(errno.EIO, "NAS disconnected")

    monkeypatch.setattr("musicscope.scanner.read_track", disconnected)
    with pytest.raises(ScanUnavailable, match="NAS disconnected"):
        scan(source, cache)
    assert cache.load_inventory(source).to_dict() == previous


def test_hash_changed_during_read_is_not_cached(source, cache, monkeypatch):
    path = audio(source / "01.flac", b"before")
    track = reader(path)
    original = os.fstat
    calls = 0

    def mutate_after_read(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            path.write_bytes(b"after with new length")
        return original(fd)

    monkeypatch.setattr("musicscope.cache.os.fstat", mutate_after_read)
    with pytest.raises(OSError, match="modifié"):
        cache.hash_track(track)
    assert cache.connection.execute("SELECT COUNT(*) FROM hashes").fetchone()[0] == 0
