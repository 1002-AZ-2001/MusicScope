"""Travail du NAS (v0.17.0) : export dans le conteneur, import sur le poste."""
import gzip
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from musicscope.cache import Cache
from musicscope.demo import create_demo
from musicscope.nas import EXPORT_NAME, _local, import_work, read_work, run_work
from musicscope.spectrum import SpectrumCache

FFMPEG = shutil.which("ffmpeg")


@pytest.fixture
def work(tmp_path):
    """Médiathèque vue par le NAS, puis copie vue par le poste (autre racine, mêmes dates)."""
    nas_root = create_demo(tmp_path / "nas" / "Musique")
    report = run_work(nas_root, tmp_path / "out", tmp_path / "nas-cache", log=lambda _: None)
    local_root = tmp_path / "poste" / "Musique"
    shutil.copytree(nas_root, local_root, copy_function=shutil.copy2)
    return report, tmp_path / "out" / EXPORT_NAME, local_root


def test_export_is_relative_and_complete(work):
    report, path, _ = work
    data = read_work(path)
    assert report.tracks == len(data["tracks"]) == 16 and report.hashed == len(data["hashes"]) > 0
    assert all(not entry["rel"].startswith("/") and ".." not in entry["rel"] for entry in data["tracks"])
    assert not path.with_name(EXPORT_NAME + ".tmp").exists()
    if FFMPEG:
        assert report.spectra == len(data["spectrum"]) > 0


def test_import_fills_the_local_caches_under_another_root(work, tmp_path):
    report, path, local_root = work
    with Cache(tmp_path / "pc.sqlite3") as cache, SpectrumCache(tmp_path / "spectre.sqlite3") as spectra:
        result = import_work(path, local_root, cache, spectra)
        assert (result.imported, result.hashes, result.missing, result.changed) == (16, report.hashed, 0, 0)
        assert result.spectra == report.spectra and result.root.endswith("Musique")
        sample = next(local_root.rglob("*.flac"))
        track = cache.get_track(str(sample), sample.stat())
        assert track is not None and track.tags  # le scan du poste ne relira pas ce fichier
        hashed = cache.connection.execute("SELECT path FROM hashes").fetchall()
        assert all(Path(row[0]).is_relative_to(local_root) for row in hashed)


def test_changed_and_missing_files_are_left_to_the_scan(work, tmp_path):
    _, path, local_root = work
    files = sorted(local_root.rglob("*.flac"))
    with open(files[0], "ab") as stream:
        stream.write(b"\0")          # taille différente
    info = files[1].stat()
    os.utime(files[1], ns=(info.st_atime_ns, info.st_mtime_ns + 5_000))  # date décalée de 5 µs
    info = files[2].stat()
    os.utime(files[2], ns=(info.st_atime_ns, info.st_mtime_ns + 500))    # 500 ns : tolérance SMB
    files[3].unlink()
    with Cache(tmp_path / "pc.sqlite3") as cache:
        result = import_work(path, local_root, cache)
    assert (result.imported, result.changed, result.missing) == (13, 2, 1)


@pytest.mark.parametrize("rel", ["../secret.flac", "/etc/passwd", "a/../../b", "a\\b", "", None, "a/\0b"])
def test_paths_outside_the_root_are_refused(tmp_path, rel):
    assert _local(tmp_path, rel) is None


def test_hostile_or_foreign_files_are_refused(tmp_path, work):
    _, path, local_root = work
    data = read_work(path)
    data["tracks"].append({"rel": "../../escape.flac", "size": 1, "mtime_ns": 1, "tags": {}})
    hostile = tmp_path / "hostile.json.gz"
    with gzip.open(hostile, "wt", encoding="utf-8") as stream:
        json.dump(data, stream)
    with Cache(tmp_path / "pc.sqlite3") as cache:
        assert import_work(hostile, local_root, cache).changed == 1
    other = tmp_path / "autre.json.gz"
    with gzip.open(other, "wt", encoding="utf-8") as stream:
        json.dump({"format": "autre", "version": 1}, stream)
    with pytest.raises(ValueError, match="compatible"):
        read_work(other)
    link = tmp_path / "lien.json.gz"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="lié"):
        read_work(link)


def test_headless_command_never_imports_qt(tmp_path):
    root = create_demo(tmp_path / "Musique")
    blocker = ("import sys, runpy; sys.modules['PySide6'] = None; sys.argv = sys.argv[1:]; "
               "runpy.run_module('musicscope', run_name='__main__')")
    command = [sys.executable, "-c", blocker, "musicscope", "--nas-work", str(root), "--out", str(tmp_path / "out"),
               "--cache", str(tmp_path / "cache"), "--spectrum", "none"]
    done = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    assert done.returncode == 0, done.stderr
    assert "16 pistes" in done.stdout and (tmp_path / "out" / EXPORT_NAME).exists()
    inside = subprocess.run(command[:6] + ["--out", str(root / "out")], capture_output=True, text=True, timeout=60, check=False)
    assert inside.returncode == 1 and "médiathèque" in inside.stderr.lower() + inside.stdout.lower()
    zero = subprocess.run(command + ["--every", "0"], capture_output=True, text=True, timeout=60, check=False)
    assert zero.returncode == 2 and "positif" in zero.stderr


def test_window_imports_then_rescans(tmp_path, work):
    from PySide6.QtWidgets import QApplication

    from musicscope.advanced_gui import AdvancedWindow
    app = QApplication.instance() or QApplication([])
    _, path, local_root = work
    window = AdvancedWindow(tmp_path / "cache")
    try:
        window.root = str(local_root)
        window.import_nas_work(str(path))
        settle(window, app)
        assert window.inventory is not None and sum(len(album.tracks) for album in window.inventory.albums) == 16
        assert any("Travail du NAS" in line and "16 piste(s)" in line for line in window.service_log)
    finally:
        window.close()


def settle(window, app, timeout=60):
    deadline, idle = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        app.processEvents()
        if window.task is None and getattr(window, "_after_task", None) is None:
            idle = idle or time.monotonic()
            if time.monotonic() - idle > 0.3:
                return
        else:
            idle = None
        time.sleep(0.01)
    raise AssertionError("tâche non terminée")
