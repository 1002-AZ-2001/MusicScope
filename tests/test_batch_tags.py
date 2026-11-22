"""Tags de plusieurs albums d'un coup (v0.20.0) : valeurs, champs vidés, rechercher / remplacer."""
import os
import shutil
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import musicscope
from musicscope.tagwriter import (
    Replacement,
    TagJournal,
    _Tags,
    batch_label,
    plan_batch_edits,
)

DATA = Path(musicscope.__file__).parent / "data"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def library(tmp_path, count=2):
    """`count` albums FLAC de deux pistes, titres « Feat. X » et commentaire."""
    from musicscope.cache import Cache
    from musicscope.scanner import scan
    for number in range(1, count + 1):
        folder = tmp_path / "lib" / "Artiste" / f"Album {number}"
        folder.mkdir(parents=True)
        for track in (1, 2):
            path = folder / f"0{track}.flac"
            shutil.copy(DATA / "tone-a.flac", path)
            tags = _Tags(path)
            for key, value in {"album": [f"Album {number}"], "albumartist": ["Artiste"], "artist": ["Artiste"],
                               "title": [f"Titre {track} Feat. Invité"], "tracknumber": [str(track)],
                               "genre": ["Rock"], "comment": ["Rip 2009"]}.items():
                tags.set(key, value)
            tags.save()
    with Cache(tmp_path / "cache.sqlite3") as cache:
        return scan(tmp_path / "lib", cache, workers=1).albums


def live(album):
    from musicscope.tag_editor_gui import read_live_values
    return read_live_values(album)


def test_values_clears_and_replacement_are_planned_for_every_album(tmp_path):
    albums = library(tmp_path)
    edits = plan_batch_edits(albums, {"genre": "Jazz", "compilation": "1"}, {"comment"},
                             Replacement("title", "feat.", "feat."), live)
    assert len(edits) == 4
    for edit in edits:
        assert edit.changes["genre"] == ["Jazz"] and edit.changes["compilation"] == ["1"]
        assert edit.changes["comment"] == [] and edit.current["comment"] == "Rip 2009"
        assert edit.changes["title"][0].endswith("feat. Invité")


def test_unchanged_files_and_absent_text_are_left_alone(tmp_path):
    albums = library(tmp_path)
    assert plan_batch_edits(albums, {"genre": "Rock"}, (), Replacement("title", "absent", "x"), live) == []
    assert plan_batch_edits(albums, {}, (), Replacement("title", "feat.", "FEAT.", case_sensitive=True), live) == []


@pytest.mark.parametrize("values,clear,replacement,message", [
    ({"title": "x"}, (), None, "plusieurs albums"),
    ({"genre": "x"}, {"genre"}, None, "rempli et vidé"),
    ({}, (), Replacement("title", "", "x"), "rechercher"),
    ({"genre": "x"}, (), Replacement("genre", "a", "b"), "soit une valeur"),
    ({}, (), Replacement("tracknumber", "1", "2"), "impossible"),
])
def test_contradictory_requests_are_refused(values, clear, replacement, message):
    with pytest.raises(ValueError, match=message):
        plan_batch_edits([], values, clear, replacement)


def test_replacement_is_literal_and_case_insensitive_by_default():
    assert Replacement("title", "a.b", "[x]").apply("A.B axb a.b") == "[x] axb [x]"
    assert Replacement("title", "\\1", "\\2").apply("x\\1y") == "x\\2y"   # aucun motif interprété
    assert Replacement("title", "Feat", "feat", True).apply("feat Feat") == "feat feat"


def test_journal_label_is_the_common_folder(tmp_path):
    albums = library(tmp_path, 3)
    assert batch_label(albums) == str(tmp_path / "lib" / "Artiste")
    assert batch_label(albums[:1]) == albums[0].path


def test_dialog_builds_the_request(app):
    from musicscope.batch_tags_gui import NO, BatchTagDialog
    dialog = BatchTagDialog([])
    try:
        dialog.inputs["genre"].setText(" Jazz ")
        dialog.inputs["comment"].setText("ignoré")
        dialog.clears["comment"].setChecked(True)
        assert not dialog.inputs["comment"].isEnabled() and dialog.inputs["comment"].text() == ""
        dialog.compilation.setCurrentText(NO)
        dialog.replace_field.setCurrentIndex(dialog.replace_field.findData("artist"))
        dialog.search.setText("Beatles")
        dialog.replacement.setText("The Beatles")
        values, clear, replacement = dialog.request()
        assert values == {"genre": "Jazz"} and clear == {"comment", "compilation"}
        assert replacement == Replacement("artist", "Beatles", "The Beatles", False)
    finally:
        dialog.deleteLater()


def test_window_writes_one_undoable_batch_for_several_albums(tmp_path, app):
    from musicscope.advanced_gui import AdvancedWindow
    library(tmp_path)
    window = AdvancedWindow(tmp_path / "cache")
    try:
        window.root = str(tmp_path / "lib")
        window.start_scan()
        settle(window, app)
        window.edit_tags_batch(request=({"genre": "Jazz"}, set(), None), confirm=True)
        assert "au moins deux albums" in window.state.text()
        window.album_table.selectAll()
        app.processEvents()
        window.edit_tags_batch(request=({"genre": "Jazz"}, {"comment"}, Replacement("title", "Feat.", "feat.")),
                               confirm=True)
        settle(window, app)
        files = sorted((tmp_path / "lib").rglob("*.flac"))
        assert len(files) == 4
        for path in files:
            tags = _Tags(path)
            assert tags.get("genre") == ["Jazz"] and tags.get("comment") == []
            assert tags.get("title")[0].endswith("feat. Invité")
        with TagJournal(window.cache_dir / "tags.sqlite3") as journal:
            [batch] = journal.batches()
            assert batch.files == 4 and batch.album_path == str(tmp_path / "lib" / "Artiste")
            assert journal.undo(batch.id).written
        for path in files:
            tags = _Tags(path)
            assert tags.get("genre") == ["Rock"] and tags.get("comment") == ["Rip 2009"]
            assert "Feat." in tags.get("title")[0]
    finally:
        window.close()


def test_batch_skips_files_shared_by_qbittorrent(tmp_path, app):
    from musicscope.advanced_gui import AdvancedWindow
    library(tmp_path)
    shared = min((tmp_path / "lib").rglob("*.flac"))
    os.link(shared, tmp_path / "seed.flac")  # lien physique : comme un import Lidarr depuis qBittorrent
    window = AdvancedWindow(tmp_path / "cache")
    try:
        window.root = str(tmp_path / "lib")
        window.start_scan()
        settle(window, app)
        window.album_table.selectAll()
        app.processEvents()
        window.edit_tags_batch(request=({"genre": "Jazz"}, set(), None), confirm=True)
        settle(window, app)
        assert _Tags(shared).get("genre") == ["Rock"]
        others = [path for path in (tmp_path / "lib").rglob("*.flac") if path != shared]
        assert all(_Tags(path).get("genre") == ["Jazz"] for path in others)
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
