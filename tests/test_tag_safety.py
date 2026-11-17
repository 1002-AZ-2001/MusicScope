"""Écriture de tags sûre (v0.11.0) : régressions issues de l'audit.

L'écriture se fait sur une copie remplacée atomiquement, ne perd aucun cadre ni total,
remplace les variantes d'un champ, et ne touche jamais un fichier lié physiquement.
"""
import hashlib
import os
import shutil
from pathlib import Path

from mutagen.flac import FLAC
from mutagen.id3 import ID3, RVA2, TALB, TDOR, TDRL, TIT2, TPE1, TSOA, TSOP, TSOT, Encoding
from mutagen.mp4 import MP4

import musicscope
from musicscope import tagwriter
from musicscope.metadata import read_track
from musicscope.tagwriter import FileEdit, TagJournal, _Tags

DATA = Path(musicscope.__file__).parent / "data"
TESTS = Path(__file__).parent / "data"
LATIN1 = Encoding.LATIN1


def edit(path, changes, journal_path):
    info = Path(path).stat()
    with TagJournal(journal_path) as journal:
        return journal, journal.apply([FileEdit(str(path), info.st_size, info.st_mtime_ns, changes, {})], "album")


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def id3v23(path, frames):
    shutil.copy(DATA / "tone-a.mp3", path)
    try:
        ID3(path).delete()
    except Exception:
        pass
    tags = ID3()
    for frame in frames:
        tags.add(frame)
    tags.save(path, v2_version=4)
    raw = bytearray(Path(path).read_bytes())
    raw[3] = 3  # en-tête ID3v2.3, cadres identiques (cas des fichiers écrits par d'autres logiciels)
    Path(path).write_bytes(bytes(raw))


def test_id3v23_keeps_sort_release_and_replaygain_frames_through_write_and_undo(tmp_path):
    path = tmp_path / "a.mp3"
    id3v23(path, [TALB(encoding=LATIN1, text="Album"), TPE1(encoding=LATIN1, text="Artist"),
                  TIT2(encoding=LATIN1, text="Title"), TSOP(encoding=LATIN1, text="Artist, The"),
                  TSOA(encoding=LATIN1, text="Album sort"), TSOT(encoding=LATIN1, text="Title sort"),
                  TDOR(encoding=LATIN1, text="1971-03-15"), TDRL(encoding=LATIN1, text="2020-01-02"),
                  RVA2(desc="track", channel=1, gain=-6.5, peak=0.9)])
    journal_path = tmp_path / "tags.sqlite3"
    _, result = edit(path, {"genre": ["Rock"]}, journal_path)
    assert result.written
    after = ID3(path)
    assert after.version[:2] == (2, 3)
    assert {"TSOP", "TSOA", "TSOT", "TDRL", "TDOR", "RVA2:track"} <= set(after.keys())
    assert str(after["TDOR"].text[0]) == "1971-03-15" and "TORY" not in after
    assert str(after["TSOP"]) == "Artist, The" and read_track(path).tags["genre"] == ["Rock"]
    with TagJournal(journal_path) as journal:
        assert journal.undo(result.batch_id).written
    assert {"TSOP", "TSOA", "TSOT", "TDRL", "TDOR"} <= set(ID3(path).keys()) and "TCON" not in ID3(path)


def test_id3v23_untouched_multi_value_field_is_not_joined(tmp_path):
    path = tmp_path / "multi.mp3"
    id3v23(path, [TPE1(encoding=LATIN1, text=["A", "B"]), TALB(encoding=LATIN1, text="X")])
    _, result = edit(path, {"album": ["Y"]}, tmp_path / "tags.sqlite3")
    assert result.written
    assert ID3(path)["TPE1"].text == ["A", "B"]


def test_m4a_track_and_disc_totals_are_kept(tmp_path):
    path = tmp_path / "a.m4a"
    shutil.copy(TESTS / "tone.m4a", path)
    tags = MP4(path)
    tags["trkn"], tags["disk"] = [(3, 12)], [(1, 2)]
    tags.save()
    journal_path = tmp_path / "tags.sqlite3"
    _, result = edit(path, {"tracknumber": ["4"], "discnumber": ["2"]}, journal_path)
    assert result.written and not result.failed
    assert MP4(path)["trkn"] == [(4, 12)] and MP4(path)["disk"] == [(2, 2)]
    with TagJournal(journal_path) as journal:
        assert journal.undo(result.batch_id).written
    assert MP4(path)["trkn"] == [(3, 12)] and MP4(path)["disk"] == [(1, 2)]


def test_vorbis_variant_is_replaced_and_restored_by_undo(tmp_path):
    path = tmp_path / "a.flac"
    shutil.copy(DATA / "tone-a.flac", path)
    tags = FLAC(path)
    tags.delete()
    tags = FLAC(path)
    tags["ALBUM ARTIST"] = ["Old"]
    tags.save()
    journal_path = tmp_path / "tags.sqlite3"
    _, result = edit(path, {"albumartist": ["New"]}, journal_path)
    assert result.written
    assert read_track(path).tags["albumartist"] == ["New"]
    assert [key.casefold() for key in FLAC(path).tags.keys()] == ["albumartist"]
    with TagJournal(journal_path) as journal:
        assert journal.undo(result.batch_id).written
    assert read_track(path).tags["albumartist"] == ["Old"]
    assert [key.casefold() for key in FLAC(path).tags.keys()] == ["album artist"]


def test_interrupted_write_never_touches_the_original(tmp_path, monkeypatch):
    path = tmp_path / "a.flac"
    shutil.copy(DATA / "tone-a.flac", path)
    before = digest(path)
    original_save = _Tags.save

    def cut(self, target=None):
        original_save(self, target)
        with open(target or self.path, "r+b") as stream:  # coupure réseau au milieu du décalage
            stream.seek(stream.seek(0, 2) // 2)
            stream.truncate()
        raise OSError(5, "coupure réseau simulée")

    monkeypatch.setattr(_Tags, "save", cut)
    # Titre plus grand que la marge réservée : mutagen doit décaler l'audio.
    journal, result = edit(path, {"title": ["y" * 20000]}, tmp_path / "tags.sqlite3")
    assert not result.written and "coupure" in result.failed[0][1]
    assert digest(path) == before
    assert not any(item.name.endswith(".musicscope-tmp") for item in tmp_path.iterdir())


def test_hardlinked_file_is_never_written(tmp_path):
    path = tmp_path / "lib" / "01.flac"
    path.parent.mkdir()
    shutil.copy(DATA / "tone-a.flac", path)
    seed = tmp_path / "seed.flac"
    os.link(path, seed)
    before = digest(seed)
    _, result = edit(path, {"genre": ["Jazz"]}, tmp_path / "tags.sqlite3")
    assert not result.written and "lié physiquement" in result.failed[0][1]
    assert digest(seed) == before and digest(path) == before
    assert tagwriter.linked_paths([FileEdit(str(path), 0, 0, {})]) == [str(path)]


def test_successful_write_replaces_the_file_and_leaves_no_working_copy(tmp_path):
    path = tmp_path / "a.flac"
    shutil.copy(DATA / "tone-a.flac", path)
    mode = path.stat().st_mode
    _, result = edit(path, {"genre": ["Jazz"]}, tmp_path / "tags.sqlite3")
    assert result.written and _Tags(path).get("genre") == ["Jazz"]
    assert path.stat().st_mode == mode
    assert not any(item.name.endswith(".musicscope-tmp") for item in tmp_path.iterdir())


def test_forgotten_working_copy_is_not_inventoried(tmp_path):
    from musicscope.cache import Cache
    from musicscope.scanner import scan
    root = tmp_path / "lib"
    (root / "Album").mkdir(parents=True)
    shutil.copy(DATA / "tone-a.flac", root / "Album" / "01.flac")
    shutil.copy(DATA / "tone-a.flac", root / "Album" / ".01.flac.0123456789ab.musicscope-tmp")
    with Cache(tmp_path / "cache.sqlite3") as cache:
        inventory = scan(root, cache, workers=1)
    assert [Path(track.path).name for album in inventory.albums for track in album.tracks] == ["01.flac"]


def test_editor_excludes_hardlinked_files_and_says_so(tmp_path):
    import time
    from PySide6.QtWidgets import QApplication
    from musicscope.advanced_gui import AdvancedWindow
    from musicscope.demo import create_demo
    app = QApplication.instance() or QApplication([])
    root = create_demo(tmp_path / "musique")
    window = AdvancedWindow(tmp_path / "cache")
    window.root = str(root)
    window.start_scan()
    deadline = time.monotonic() + 30
    while window.task is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    try:
        row = next(index for index, album in enumerate(window.inventory.albums) if album.path.endswith("Horizons FLAC"))
        window.album_table.selectRow(window.album_proxy.mapFromSource(window.album_model.index(row, 0)).row())
        app.processEvents()
        before = [digest(track.path) for track in window.inventory.albums[row].tracks]
        window.edit_tags(dialog_values=({"genre": "Jazz"}, {}), confirm=True)
        assert window.task is None and "lié" in window.state.text()
        assert [digest(track.path) for track in window.inventory.albums[row].tracks] == before
    finally:
        window.close()
