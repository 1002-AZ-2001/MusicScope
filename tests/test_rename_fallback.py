"""Partages SMB/NFS : renameat2(RENAME_NOREPLACE) y renvoie EINVAL ; le repli ne doit rien écraser."""
from __future__ import annotations

import errno
import os

import pytest

from musicscope import organizer
from musicscope.cache import Cache
from musicscope.file_duplicates import CONTENT, ScanOptions, collect_files, find_duplicates, quarantine_files, restore_file_journal
from musicscope.organizer import QUARANTINE_DIR, _rename_without_replace, apply_plan, plan_quarantine, restore_journal
from musicscope.scanner import scan

linux_only = pytest.mark.skipif(not os.sys.platform.startswith("linux"), reason="renameat2 Linux")


@pytest.fixture
def no_noreplace(monkeypatch):
    """Simule un montage CIFS/NFS : RENAME_NOREPLACE refusé avec EINVAL."""
    calls = []

    def unsupported(*args):
        calls.append(args)
        return errno.EINVAL

    monkeypatch.setattr(organizer, "_renameat2_noreplace", unsupported)
    return calls


@linux_only
@pytest.mark.parametrize("is_dir", [True, False])
def test_fallback_moves_directory_and_file(tmp_path, no_noreplace, is_dir):
    source = tmp_path / "src"
    if is_dir:
        source.mkdir()
        (source / "01.flac").write_bytes(b"audio")
    else:
        source.write_bytes(b"audio")
    info = source.lstat()
    destination = tmp_path / "dest" / "moved"
    destination.parent.mkdir()
    _rename_without_replace(source, destination, (info.st_dev, info.st_ino), is_dir=is_dir)
    assert no_noreplace and not source.exists()
    assert (destination / "01.flac").read_bytes() == b"audio" if is_dir else destination.read_bytes() == b"audio"


@linux_only
@pytest.mark.parametrize("is_dir", [True, False])
def test_fallback_never_replaces_existing_destination(tmp_path, no_noreplace, is_dir):
    source = tmp_path / "src"
    if is_dir:
        source.mkdir()
    else:
        source.write_bytes(b"nouveau")
    destination = tmp_path / "existant"
    if is_dir:
        destination.mkdir()  # même un dossier vide existant n'est pas remplacé
        (destination / "garde.txt").write_bytes(b"x")
    else:
        destination.write_bytes(b"precieux")
    info = source.lstat()
    with pytest.raises(FileExistsError):
        _rename_without_replace(source, destination, (info.st_dev, info.st_ino), is_dir=is_dir)
    assert source.exists()
    assert (destination / "garde.txt").exists() if is_dir else destination.read_bytes() == b"precieux"


@linux_only
def test_failed_rename_removes_only_its_reservation(tmp_path, no_noreplace, monkeypatch):
    source = tmp_path / "src.flac"
    source.write_bytes(b"audio")
    destination = tmp_path / "dest.flac"

    def broken(*args, **kwargs):
        raise OSError(errno.EIO, "coupure réseau")

    monkeypatch.setattr(organizer.os, "rename", broken)
    info = source.lstat()
    with pytest.raises(OSError):
        _rename_without_replace(source, destination, (info.st_dev, info.st_ino), is_dir=False)
    assert source.read_bytes() == b"audio" and not destination.exists()


@linux_only
def test_other_errors_are_not_masked(tmp_path, monkeypatch):
    monkeypatch.setattr(organizer, "_renameat2_noreplace", lambda *args: errno.EACCES)
    source = tmp_path / "src"
    source.mkdir()
    info = source.lstat()
    with pytest.raises(PermissionError):
        _rename_without_replace(source, tmp_path / "dest", (info.st_dev, info.st_ino))
    assert source.exists()


@linux_only
def test_album_quarantine_and_restore_on_network_like_mount(tmp_path, no_noreplace):
    root = tmp_path / "Musique"
    album = root / "desert sand feels warm at night" / "0 2 1 (2019)"
    album.mkdir(parents=True)
    (album / "01.flac").write_bytes(b"audio")
    from musicscope.models import Track
    with Cache(tmp_path / "c.sqlite3") as cache:
        import musicscope.scanner as scanner_module
        original = scanner_module.read_track
        scanner_module.read_track = lambda path, stat: Track(str(path), "FLAC", stat.st_size, stat.st_mtime_ns, 1.0,
                                                             {"album": ["0 : 2 1"]})
        try:
            inventory = scan(root, cache)
        finally:
            scanner_module.read_track = original
    plan = plan_quarantine(inventory.albums[0], root)
    assert not plan.blockers, plan.blockers
    journal = apply_plan(plan, tmp_path / "journals", confirmed=True)
    assert not album.exists() and (root / QUARANTINE_DIR / "desert sand feels warm at night" / "0 2 1 (2019)" / "01.flac").exists()
    restore_journal(journal, confirmed=True)
    assert (album / "01.flac").read_bytes() == b"audio"


@linux_only
def test_file_quarantine_on_network_like_mount(tmp_path, no_noreplace):
    root = tmp_path / "Musique"
    for folder in ("A", "B"):
        (root / folder).mkdir(parents=True)
        (root / folder / "cover.jpg").write_bytes(b"image")
    options = ScanOptions(frozenset({CONTENT}), min_size=1)
    with Cache(tmp_path / "c.sqlite3") as cache:
        group = find_duplicates(collect_files(root, options), options, cache=cache)[0]
    journal, moved, failed = quarantine_files([group.files[1]], root, tmp_path / "journals")
    assert not failed and moved == [str(root / "B" / "cover.jpg")]
    restored, failed = restore_file_journal(journal)
    assert not failed and (root / "B" / "cover.jpg").exists()
