"""Éditeur de tags (v0.15.0) : champs étendus et pochette, intégrée et en fichier."""
import os
import shutil
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

import musicscope
from musicscope.metadata import read_track
from musicscope.tagwriter import TagJournal, _Tags, cover_file_edit, plan_album_edits

DATA = Path(musicscope.__file__).parent / "data"
TESTS = Path(__file__).parent / "data"
SOURCES = {"flac": DATA / "tone-a.flac", "mp3": DATA / "tone-a.mp3", "m4a": TESTS / "tone.m4a",
           "ogg": TESTS / "tone.ogg", "opus": TESTS / "tone.opus"}
MBID = "8f3c2d4a-1b2c-4d5e-8f90-123456789abc"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def image(app, color="red", form="PNG", size=64) -> bytes:
    picture = QImage(size, size, QImage.Format.Format_RGB32)
    picture.fill(QColor(color))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    picture.save(buffer, form)
    return bytes(data)


def album_with(tmp_path, fmt):
    from musicscope.cache import Cache
    from musicscope.scanner import scan
    folder = tmp_path / "lib" / "Artiste" / "Album"
    folder.mkdir(parents=True)
    path = folder / f"01.{fmt}"
    shutil.copy(SOURCES[fmt], path)
    tags = _Tags(path)
    for key, value in {"album": ["Album"], "albumartist": ["Artiste"], "title": ["Titre"], "tracknumber": ["3"],
                       "artist": ["Artiste"]}.items():
        tags.set(key, value)
    tags.save()
    with Cache(tmp_path / "cache.sqlite3") as cache:
        return scan(tmp_path / "lib", cache, workers=1).albums[0]


def apply(tmp_path, album, album_values, track_values=None, images=()):
    from musicscope.tag_editor_gui import read_live_values
    edits = plan_album_edits(album, album_values, track_values or {}, read_live_values(album))
    with TagJournal(tmp_path / "tags.sqlite3") as journal:
        for data in images:
            journal.store_image(data)
        return journal, journal.apply(edits, album.path)


@pytest.mark.parametrize("fmt", list(SOURCES))
def test_extended_fields_round_trip_and_undo(tmp_path, fmt):
    album = album_with(tmp_path, fmt)
    track = album.tracks[0]
    values = {"tracktotal": "12", "compilation": "1", "composer": "Ravel", "comment": "Remaster",
              "musicbrainz_albumid": MBID, "musicbrainz_trackid": MBID, "conductor": "Boulez"}
    _, result = apply(tmp_path, album, values, {track.path: {"lyrics": "Ligne un\nLigne deux",
                                                                    "performer": "Orchestre"}})
    assert result.written and not result.failed, result.failed
    tags = _Tags(Path(track.path))
    assert tags.get("tracktotal") == ["12"] and tags.get("tracknumber")[0].startswith("3")
    assert tags.get("compilation") == ["1"] and tags.get("composer") == ["Ravel"]
    assert tags.get("comment") == ["Remaster"] and tags.get("lyrics") == ["Ligne un\nLigne deux"]
    assert tags.get("musicbrainz_trackid") == [MBID]
    if fmt == "m4a":
        assert tags.get("conductor") == [] and tags.get("performer") == []  # absents du format : ignorés
    else:
        assert tags.get("conductor") == ["Boulez"] and tags.get("performer") == ["Orchestre"]
    scanned = read_track(Path(track.path)).tags  # le scanner relit les mêmes valeurs
    assert scanned.get("tracktotal") == ["12"] and scanned.get("musicbrainz_albumid") == [MBID]
    with TagJournal(tmp_path / "tags.sqlite3") as journal:
        undo = journal.undo(result.batch_id)
    assert undo.written and not undo.failed, undo.failed
    tags = _Tags(Path(track.path))
    assert tags.get("composer") == [] and tags.get("tracktotal") == [] and tags.get("lyrics") == []
    assert tags.get("tracknumber")[0] == "3"


def test_mp3_total_lives_in_the_track_frame(tmp_path):
    from mutagen.id3 import ID3
    album = album_with(tmp_path, "mp3")
    apply(tmp_path, album, {"tracktotal": "14"})
    assert str(ID3(album.tracks[0].path)["TRCK"]) == "3/14"


@pytest.mark.parametrize("fmt", list(SOURCES))
def test_embedded_cover_is_replaced_then_restored(tmp_path, app, fmt):
    album = album_with(tmp_path, fmt)
    path = Path(album.tracks[0].path)
    from musicscope.cache import Cache
    from musicscope.scanner import scan
    from musicscope.tagfields import image_reference
    first, second = image(app, "red", "PNG"), image(app, "blue", "JPG")
    _, result = apply(tmp_path, album, {"artwork": image_reference(first)}, images=[first])
    assert result.written, result.failed
    assert _Tags(path).covers()[0][1] == first
    with Cache(tmp_path / "cache.sqlite3") as cache:  # nouvel inventaire : taille et date ont changé
        album = scan(tmp_path / "lib", cache, workers=1).albums[0]
    _, result = apply(tmp_path, album, {"artwork": image_reference(second)}, images=[second])
    assert result.written, result.failed
    covers = _Tags(path).covers()
    assert len(covers) == 1 and covers[0] == ("image/jpeg", second)
    with TagJournal(tmp_path / "tags.sqlite3") as journal:
        assert journal.undo(result.batch_id).written
    assert _Tags(path).covers()[0][1] == first  # pochette précédente remise depuis le journal


def test_cover_file_is_written_and_undone(tmp_path, app):
    album = album_with(tmp_path, "flac")
    data = image(app, "green", "JPG")
    from musicscope.tagfields import image_reference
    edit = cover_file_edit(album, image_reference(data))
    assert Path(edit.path).name == "cover.jpg"
    with TagJournal(tmp_path / "tags.sqlite3") as journal:
        journal.store_image(data)
        result = journal.apply([edit], album.path)
        assert result.written and (Path(album.path) / "cover.jpg").read_bytes() == data
        assert journal.undo(result.batch_id).written
    assert not (Path(album.path) / "cover.jpg").exists()


def test_existing_cover_file_that_is_hardlinked_is_never_replaced(tmp_path, app):
    album = album_with(tmp_path, "flac")
    cover = Path(album.path) / "cover.jpg"
    cover.write_bytes(image(app, "red", "JPG"))
    os.link(cover, tmp_path / "seed-cover.jpg")
    before = cover.read_bytes()
    data = image(app, "blue", "JPG")
    from musicscope.tagfields import image_reference
    with TagJournal(tmp_path / "tags.sqlite3") as journal:
        journal.store_image(data)
        result = journal.apply([cover_file_edit(album, image_reference(data))], album.path)
    assert not result.written and "lié physiquement" in result.failed[0][1]
    assert cover.read_bytes() == before


@pytest.mark.parametrize("values,message", [({"musicbrainz_albumid": "pas-un-uuid"}, "MBID"),
                                            ({"tracktotal": "douze"}, "entier")])
def test_invalid_values_are_refused_before_writing(tmp_path, values, message):
    album = album_with(tmp_path, "flac")
    with pytest.raises(ValueError, match=message):
        plan_album_edits(album, values, {})


def test_image_validation():
    from musicscope.tagfields import image_reference
    with pytest.raises(ValueError, match="JPEG ou PNG"):
        image_reference(b"GIF89a....")
    with pytest.raises(ValueError, match="trop volumineuse"):
        image_reference(b"\xff\xd8\xff" + bytes(17 * 1024 * 1024))


def test_dialog_reads_live_values_and_builds_cover_edits(tmp_path, app):
    from musicscope.tag_editor_gui import TagEditorDialog
    album = album_with(tmp_path, "flac")
    apply(tmp_path, album, {"composer": "Ravel"})
    from musicscope.cache import Cache
    from musicscope.scanner import scan
    with Cache(tmp_path / "cache.sqlite3") as cache:
        album = scan(tmp_path / "lib", cache, workers=1).albums[0]
    dialog = TagEditorDialog(album)
    try:
        assert dialog.album_inputs["composer"].text() == "Ravel"  # lu dans le fichier
        assert dialog.edits() == []                                 # rien de changé : rien à écrire
        dialog.set_image(image(app, "red", "JPG"), source="test.jpg")
        dialog.compilation.setCurrentText("Oui")
        edits = dialog.edits()
        assert {key for edit in edits for key in edit.changes} == {"artwork", "compilation", "coverfile"}
        assert Path(edits[-1].path).name == "cover.jpg"
    finally:
        dialog.deleteLater()


def test_window_writes_cover_and_tags_then_rescans(tmp_path, app):
    from musicscope.advanced_gui import AdvancedWindow
    from musicscope.demo import create_demo
    root = create_demo(tmp_path / "musique")
    window = AdvancedWindow(tmp_path / "cache")
    try:
        window.root = str(root)
        window.start_scan()
        settle(window, app)
        row = next(index for index, album in enumerate(window.inventory.albums) if album.path.endswith("Horizons MP3"))
        window.album_table.selectRow(window.album_proxy.mapFromSource(window.album_model.index(row, 0)).row())
        app.processEvents()
        data = image(app, "purple", "JPG")
        window.edit_tags(dialog_values=({"composer": "Compositrice"}, {}), confirm=True, image_data=data,
                         cover_file=True)
        settle(window, app)
        folder = root / "Artiste Démo" / "Horizons MP3"
        assert (folder / "cover.jpg").read_bytes() == data
        assert all(_Tags(path).covers()[0][1] == data for path in folder.glob("*.mp3"))
        assert window.tag_batch_model.rowCount() == 1
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
