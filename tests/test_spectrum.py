"""Authenticité des FLAC (v0.14.0) : coupure d'encodeur avec perte détectée par FFmpeg."""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from musicscope import spectrum
from musicscope.models import Album, Track
from musicscope.spectrum import CENTERS, GENUINE, REVIEW, SUSPECT, UNKNOWN, SpectrumCache, SpectrumResult, interpret

FFMPEG = shutil.which("ffmpeg")


def profile(cutoff_index=None, start=-52.0, slope=-1.2):
    """Niveaux des bandes : pente douce, puis silence numérique à partir de `cutoff_index`."""
    return [(-91.0 if cutoff_index is not None and index >= cutoff_index else start + slope * index)
            for index in range(len(CENTERS))]


@pytest.mark.parametrize("bands,rate,verdict,cutoff", [
    (profile(), 44100, GENUINE, None),
    (profile(5), 44100, SUSPECT, 17.0),        # MP3 128 kb/s : silence dès la bande 17,5 kHz
    (profile(7), 44100, REVIEW, 19.0),         # MP3 V2
    (profile(8), 48000, REVIEW, 20.0),         # MP3 320 kb/s
    (profile(slope=-6.0), 44100, GENUINE, None),  # master sombre : atténuation progressive, pas de falaise
    (profile(start=-85.0), 44100, UNKNOWN, None),  # passage trop calme
    (profile(5), 32000, UNKNOWN, None),        # 32 kHz : pas d'aigus au-delà de 16 kHz, aucune conclusion
])
def test_interpretation_of_band_levels(bands, rate, verdict, cutoff):
    result = interpret(bands, rate)
    assert result.verdict == verdict and result.cutoff_khz == cutoff


def test_cache_returns_results_only_for_unchanged_files(tmp_path):
    track = Track(str(tmp_path / "a.flac"), "FLAC", 10, 1)
    with SpectrumCache(tmp_path / "spectrum.sqlite3") as cache:
        cache.put(track, SpectrumResult(SUSPECT, "coupure", 17.0, [1.0]))
        assert cache.get(track).cutoff_khz == 17.0
        assert cache.get(Track(track.path, "FLAC", 11, 1)) is None
        from musicscope.models import Inventory
        known = cache.known(Inventory(str(tmp_path), [Album(str(tmp_path), [track, Track("/b.mp3", "MP3")])]))
        assert list(known) == [track.path]


def test_sample_takes_central_lossless_tracks():
    tracks = [Track(f"/a/{number:02d}.flac", "FLAC", tags={"tracknumber": [str(number)]}) for number in range(1, 11)]
    tracks.append(Track("/a/bonus.mp3", "MP3", tags={"tracknumber": ["11"]}))
    album = Album("/a", tracks)
    assert [Path(track.path).name for track in spectrum.sample_tracks(album, 1)] == ["06.flac"]
    assert len(spectrum.sample_tracks(album, None)) == 10


def test_suspect_flac_is_never_the_recommended_copy():
    from musicscope.duplicates import score_album
    from musicscope import file_duplicates as fd
    tags = {"album": ["A"], "albumartist": ["B"], "title": ["T"], "tracknumber": ["1"], "date": ["2020"]}
    flac = Album("/lib/A (FLAC)", [Track("/lib/A (FLAC)/01.flac", "FLAC", 30_000_000, 1, 200.0, dict(tags))])
    mp3 = Album("/lib/A (MP3)", [Track("/lib/A (MP3)/01.mp3", "MP3", 8_000_000, 1, 200.0, dict(tags))])
    spectra = {"/lib/A (FLAC)/01.flac": SpectrumResult(SUSPECT, "coupure", 17.0)}
    scores = [score_album(flac, [], spectra), score_album(mp3, [])]
    assert scores[1].score > scores[0].score
    assert any("FLAC suspect" in weakness for weakness in scores[0].weaknesses)
    genuine = score_album(flac, [], {"/lib/A (FLAC)/01.flac": SpectrumResult(GENUINE, "ok")})
    assert genuine.score > scores[1].score and any("spectre complet" in item for item in genuine.strengths)
    records = [fd.FileRecord("/x/a.flac", 30_000_000, 1, 1, 1, flac.tracks[0], spectrum=SUSPECT),
               fd.FileRecord("/x/a.mp3", 8_000_000, 1, 1, 2, mp3.tracks[0])]
    assert fd.FileGroup(records, (fd.SONG,)).best().path == "/x/a.mp3"


# Avec FFmpeg
def encoders():
    if FFMPEG is None:
        return set()
    output = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return {line.split()[1] for line in output.splitlines()[10:] if len(line.split()) > 1}


needs_lame = pytest.mark.skipif(FFMPEG is None or "libmp3lame" not in encoders(), reason="FFmpeg avec libmp3lame requis")


def make(path, source=None, bitrate=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    if source is None:
        subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "anoisesrc=d=40:c=pink:r=44100:a=0.3",
                        "-ac", "2", str(path)], check=True)
    else:
        lossy = path.with_suffix(".mp3")
        subprocess.run([FFMPEG, "-v", "error", "-i", str(source), "-c:a", "libmp3lame", "-b:a", bitrate, str(lossy)],
                       check=True)
        subprocess.run([FFMPEG, "-v", "error", "-i", str(lossy), str(path)], check=True)
        lossy.unlink()
    return path


@needs_lame
def test_real_ffmpeg_flags_a_flac_made_from_mp3(tmp_path):
    real = make(tmp_path / "real.flac")
    fake = make(tmp_path / "fake.flac", real, "128k")
    for path, verdict in ((real, GENUINE), (fake, SUSPECT)):
        bands = spectrum.measure(str(path), 40.0, FFMPEG)
        assert interpret(bands, 44100).verdict == verdict, (path.name, bands)


@needs_lame
def test_window_analysis_feeds_quality_page_and_duplicates(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from mutagen.flac import FLAC
    from musicscope.advanced_gui import AdvancedWindow
    app = QApplication.instance() or QApplication([])
    root = tmp_path / "lib"
    real = make(root / "Artiste" / "Album (2020)" / "01 - Titre.flac")
    make(root / "Téléchargements" / "Album (2020)" / "01 - Titre.flac", real, "128k")
    for path in root.rglob("*.flac"):
        tags = FLAC(path)
        tags.update({"album": "Album", "albumartist": "Artiste", "artist": "Artiste", "title": "Titre",
                     "tracknumber": "1", "date": "2020"})
        tags.save()
    window = AdvancedWindow(tmp_path / "cache")
    try:
        window.root = str(root)
        window.start_scan()
        settle(window, app)
        window.check_spectrum(scope=1)
        settle(window, app)
        assert "1 suspecte" in window.state.text()
        rows = [window.quality_model.item(row, 1).text() for row in range(window.quality_model.rowCount())]
        assert "Spectre" in rows
        groups = [group for group in window.duplicate_groups if len(group.albums) == 2]
        if groups:
            kept = groups[0].albums[groups[0].keep] if groups[0].keep is not None else None
            assert kept is None or "Téléchargements" not in kept.path
        # Résultats gardés en cache : relus au redémarrage sans réanalyse.
        window.start_scan()
        settle(window, app)
        assert any(result.verdict == SUSPECT for result in window.spectra.values())
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


def test_missing_ffmpeg_gives_a_clear_message(monkeypatch):
    monkeypatch.setattr(spectrum.shutil, "which", lambda name: None)
    with pytest.raises(spectrum.SpectrumUnavailable, match="FFmpeg est introuvable"):
        spectrum.analyze([Album("/a", [Track("/a/1.flac", "FLAC")])], None)
