"""Éditeur de tags dans l'interface : saisie, aperçu, écriture, actualisation et annulation."""
import os
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QItemSelectionModel
from PySide6.QtWidgets import QApplication

from musicscope.advanced_gui import AdvancedWindow
from musicscope.tag_editor_gui import MIXED, TagEditorDialog, TagPreviewDialog
from musicscope.tagwriter import _Tags

from test_tagwriter import make_album


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


def settle(window, app, timeout=20):
    """Attend la tâche en cours et celles qu'elle enchaîne (actualisation)."""
    deadline = time.monotonic() + timeout
    idle_since = None
    while time.monotonic() < deadline:
        app.processEvents()
        if window.task is None:
            idle_since = idle_since or time.monotonic()
            if time.monotonic() - idle_since > 0.3:
                return
        else:
            idle_since = None
        time.sleep(0.01)
    raise AssertionError("tâche non terminée")


@pytest.fixture
def window(tmp_path, app):
    album = make_album(tmp_path, ("flac", "mp3", "ogg"))
    widget = AdvancedWindow(tmp_path / "gui-cache")
    widget.root = str(tmp_path / "library")
    widget.start_scan()
    settle(widget, app)
    assert widget.inventory.albums and Path(widget.inventory.albums[0].path) == Path(album.path)
    yield widget
    if widget.task:
        widget.task.cancel.set()
        settle(widget, app)
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def select_first_album(window):
    index = window.album_proxy.index(0, 0)
    window.album_table.selectionModel().select(
        index, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)


def test_edit_write_rescan_and_undo(window, app):
    select_first_album(window)
    first = window.inventory.albums[0].ordered_tracks[0]
    window.edit_tags(dialog_values=({"album": "Album corrigé"}, {first.path: {"title": "Ouverture"}}), confirm=True)
    settle(window, app)
    assert [_Tags(Path(t.path)).get("album") for t in window.inventory.albums[0].ordered_tracks] == [["Album corrigé"]] * 3
    # L'actualisation automatique a relu les fichiers modifiés.
    assert not window.requires_rescan
    assert window.inventory.albums[0].value("album") == "Album corrigé"
    assert window.inventory.albums[0].ordered_tracks[0].value("title") == "Ouverture"
    assert window.tabs.currentIndex() == window.tags_index
    assert window.tag_batch_model.rowCount() == 1
    assert window.tag_batch_model.item(0, 3).text() == "3/3"
    assert "Ancien album" in window.tag_details.toPlainText()
    batch_id = window.tag_batch_model.item(0, 0).data(0x0100)
    window.undo_tags(batch_id, confirm=True)
    settle(window, app)
    assert window.inventory.albums[0].value("album") == "Ancien album"
    assert window.tag_batch_model.item(0, 4).text().startswith("Annulée")


def test_refused_or_unchanged_edits_write_nothing(window, app):
    select_first_album(window)
    path = window.inventory.albums[0].ordered_tracks[0].path
    window.edit_tags(dialog_values=({"album": "Autre"}, {}), confirm=False)
    window.edit_tags(dialog_values=({"album": "Ancien album"}, {}), confirm=True)
    settle(window, app)
    assert "rien à écrire" in window.state.text()
    assert _Tags(Path(path)).get("album") == ["Ancien album"]
    assert window.tag_batch_model.rowCount() == 0
    window.historical = True
    window.edit_tags(dialog_values=({"album": "Autre"}, {}), confirm=True)
    assert window.task is None and "actualiser" in window.state.text()


def test_context_menu_offers_tag_editing(window):
    album = window.inventory.albums[0]
    assert ("Modifier les tags…", True) in [(title, enabled) for title, _, enabled in window.album_actions([album])]
    assert any(action.text().startswith("Modifier les tags") and action.shortcut().toString() == "Ctrl+T"
               for menu in window.menuBar().actions() if menu.text() == "Tags" for action in menu.menu().actions())


def test_dialog_values_mixed_fields_and_proposals(window, app):
    album = window.inventory.albums[0]
    tracks = album.ordered_tracks
    dialog = TagEditorDialog(album)
    try:
        assert dialog.album_inputs["album"].text() == "Ancien album"
        assert not dialog.proposal_button.isEnabled()
        assert dialog.edits() == []
        dialog.table.item(1, 3).setText("Titre modifié")
        edits = dialog.edits()
        assert [(Path(e.path).name, e.changes) for e in edits] == [("02.mp3", {"title": ["Titre modifié"]})]
    finally:
        dialog.deleteLater()
    tracks[0].tags["genre"] = ["Rock"]
    tracks[1].tags["genre"] = ["Jazz"]
    proposals = [SimpleNamespace(path=t.path, field="album", proposed="Vrai album") for t in tracks]
    proposals.append(SimpleNamespace(path=tracks[2].path, field="title", proposed="Vrai titre"))
    dialog = TagEditorDialog(album, proposals)
    try:
        assert dialog.album_inputs["genre"].placeholderText() == MIXED
        assert dialog.edits() == []  # un champ mixte non touché est conservé
        dialog.apply_proposals()
        changes = {Path(e.path).name: e.changes for e in dialog.edits()}
        assert changes["01.flac"] == {"album": ["Vrai album"]}
        assert changes["03.ogg"] == {"album": ["Vrai album"], "title": ["Vrai titre"]}
    finally:
        dialog.deleteLater()


def test_preview_lists_every_change(window):
    album = window.inventory.albums[0]
    from musicscope.tagwriter import plan_album_edits
    edits = plan_album_edits(album, {"albumartist": ""}, {album.ordered_tracks[0].path: {"title": "X"}})
    dialog = TagPreviewDialog(edits)
    try:
        assert dialog.table.rowCount() == 4
        cells = [dialog.table.item(row, 3).text() for row in range(dialog.table.rowCount())]
        assert cells.count("(supprimé)") == 3 and "X" in cells
        assert "3 fichier(s)" in dialog.write_button.text()
    finally:
        dialog.deleteLater()
