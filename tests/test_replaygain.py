"""ReplayGain 2.0 par FFmpeg et import automatique du travail du NAS (v0.22.0)."""
import os
import shutil
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import musicscope
from musicscope.replaygain import (
    Loudness,
    _parse,
    measure_album,
    missing_replaygain,
    tag_values,
)
from musicscope.tagwriter import TagJournal, _Tags

DATA = Path(musicscope.__file__).parent / "data"
TESTS = Path(__file__).parent / "data"
FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="FFmpeg requis")

OUTPUT = """[out#0/null @ 0x1] video:0kB audio:188kB
[ebur128@album @ 0x5] Summary:

  Integrated loudness:
    I:         -21.7 LUFS
    Threshold: -31.7 LUFS

  Sample peak:
    Peak:      -18.1 dBFS
[ebur128@piste1 @ 0x4] Summary:

  Integrated loudness:
    I:         -70.0 LUFS
    Threshold: -70.0 LUFS

  Sample peak:
    Peak:       -inf dBFS
[ebur128@piste0 @ 0x3] Summary:

  Integrated loudness:
    I:          -9.4 LUFS

  Sample peak:
    Peak:       0.3 dBFS
"""


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_summaries_are_read_by_name_whatever_their_order():
    found = _parse(OUTPUT)
    assert found["piste0"] == Loudness(-9.4, 0.3) and found["album"] == Loudness(-21.7, -18.1)
    assert found["piste1"].silent and found["piste1"].peak_db == -200.0


def test_tag_values_follow_replaygain_2_and_opus_rules():
    loud, album = Loudness(-9.4, 0.3), Loudness(-12.0, -1.0)
    values = tag_values(loud, album, "FLAC")
    assert values == {"replaygain_track_gain": "-8.60 dB", "replaygain_track_peak": f"{10 ** (0.3 / 20):.6f}",
                      "replaygain_album_gain": "-6.00 dB", "replaygain_album_peak": f"{10 ** (-1 / 20):.6f}"}
    # Opus : R128 relatif à -23 LUFS, en 1/256 dB (RFC 7845)
    assert tag_values(loud, album, "Opus") == {"r128_track_gain": str(round(-13.6 * 256)),
                                               "r128_album_gain": str(-11 * 256)}
    assert tag_values(Loudness(-70.0, -200.0), album, "FLAC") == {}  # piste muette : aucun gain


@needs_ffmpeg
def test_real_measure_of_an_album_in_one_pass():
    tracks, album = measure_album([str(DATA / "tone-a.flac"), str(DATA / "tone-b.flac")], FFMPEG)
    assert [round(track.integrated, 1) for track in tracks] == [-21.8, -21.6]
    assert tracks[0].integrated <= album.integrated <= tracks[1].integrated
    assert tag_values(tracks[0], album, "FLAC")["replaygain_track_gain"] == "3.80 dB"


@needs_ffmpeg
def test_undecodable_file_is_explained(tmp_path):
    from musicscope.replaygain import ReplayGainUnavailable
    broken = tmp_path / "abime.flac"
    broken.write_bytes(b"fLaC" + bytes(100))
    with pytest.raises(ReplayGainUnavailable, match="impossible"):
        measure_album([str(broken)], FFMPEG)


def library(tmp_path, names=("Album A", "Album B")):
    from musicscope.cache import Cache
    from musicscope.scanner import scan
    for name in names:
        folder = tmp_path / "lib" / "Artiste" / name
        folder.mkdir(parents=True)
        for number, source in enumerate((DATA / "tone-a.flac", DATA / "tone-b.mp3"), 1):
            path = folder / f"0{number}{source.suffix}"
            shutil.copy(source, path)
            tags = _Tags(path)
            for key, value in {"album": [name], "albumartist": ["Artiste"], "title": [f"T{number}"],
                               "tracknumber": [str(number)]}.items():
                tags.set(key, value)
            tags.save()
    with Cache(tmp_path / "cache.sqlite3") as cache:
        return sorted(scan(tmp_path / "lib", cache, workers=1).albums, key=lambda album: album.path)


def open_window(tmp_path, app):
    from musicscope.advanced_gui import AdvancedWindow
    window = AdvancedWindow(tmp_path / "cache")
    window.root = str(tmp_path / "lib")
    window.start_scan()
    settle(window, app)
    return window


@needs_ffmpeg
def test_window_writes_replaygain_of_a_selected_album_then_undo(tmp_path, app):
    library(tmp_path)
    window = open_window(tmp_path, app)
    try:
        row = next(index for index, album in enumerate(window.inventory.albums) if album.path.endswith("Album A"))
        window.album_table.selectRow(window.album_proxy.mapFromSource(window.album_model.index(row, 0)).row())
        app.processEvents()
        window.compute_replaygain(confirm=True)
        settle(window, app)
        folder = tmp_path / "lib" / "Artiste" / "Album A"
        flac, mp3 = _Tags(folder / "01.flac"), _Tags(folder / "02.mp3")
        assert flac.get("replaygain_track_gain") == ["3.80 dB"] and flac.get("replaygain_album_gain")
        assert mp3.get("replaygain_track_gain") and mp3.get("replaygain_album_gain") == flac.get("replaygain_album_gain")
        assert _Tags(tmp_path / "lib" / "Artiste" / "Album B" / "01.flac").get("replaygain_track_gain") == []
        with TagJournal(window.cache_dir / "tags.sqlite3") as journal:
            [batch] = journal.batches()
            assert journal.undo(batch.id).written
        assert _Tags(folder / "01.flac").get("replaygain_track_gain") == []
    finally:
        window.close()


@needs_ffmpeg
def test_whole_library_only_measures_albums_without_replaygain(tmp_path, app):
    albums = library(tmp_path)
    assert all(missing_replaygain(album) for album in albums)
    done = tmp_path / "lib" / "Artiste" / "Album A"
    for path in done.iterdir():
        tags = _Tags(path)
        tags.set("replaygain_track_gain", ["-1.00 dB"])
        tags.save()
    window = open_window(tmp_path, app)
    try:
        window.compute_replaygain(confirm=True, ask=True)
        settle(window, app)
        assert _Tags(done / "01.flac").get("replaygain_track_gain") == ["-1.00 dB"]  # déjà calculé : gardé
        assert _Tags(tmp_path / "lib" / "Artiste" / "Album B" / "01.flac").get("replaygain_track_gain")
        assert not missing_replaygain(next(a for a in window.inventory.albums if a.path.endswith("Album B")))
    finally:
        window.close()


def test_missing_ffmpeg_is_explained(tmp_path, app, monkeypatch):
    from musicscope import spectrum
    library(tmp_path, ("Album A",))
    window = open_window(tmp_path, app)
    try:
        monkeypatch.setattr(spectrum.shutil, "which", lambda name: None)
        window.album_table.selectAll()
        window.compute_replaygain(confirm=True)
        assert "FFmpeg est introuvable" in window.state.text()
    finally:
        window.close()


def test_nas_work_is_imported_again_at_startup_only_when_it_changed(tmp_path, app):
    from musicscope.advanced_gui import AdvancedWindow
    from musicscope.demo import create_demo
    from musicscope.nas import EXPORT_NAME, run_work
    root = create_demo(tmp_path / "Musique")
    run_work(root, tmp_path / "out", tmp_path / "nas-cache", spectrum="none", log=lambda _: None)
    export = tmp_path / "out" / EXPORT_NAME
    window = AdvancedWindow(tmp_path / "cache")
    try:
        window.root = str(root)
        assert not window.auto_import_nas()                 # jamais importé : rien à faire
        window.import_nas_work(str(export))
        settle(window, app)
        assert window._preferences()["nas_work"]["path"] == str(export)
        assert not window.auto_import_nas()                 # inchangé depuis l'import
        info = export.stat()
        os.utime(export, ns=(info.st_atime_ns, info.st_mtime_ns + 10**9))  # nouveau passage du NAS
        assert window.auto_import_nas()
        settle(window, app)
        assert window.service_log[-1].startswith("Import automatique")
        os.utime(export, ns=(info.st_atime_ns, info.st_mtime_ns + 2 * 10**9))
        window.auto_nas_action.setChecked(False)
        assert not window.auto_import_nas()                 # désactivé dans le menu Services
    finally:
        window.close()


def settle(window, app, timeout=120):
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
