"""Navigation latérale, menus, raccourcis, chemins relatifs et préférences de fenêtre."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QLocale, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

from musicscope.advanced_gui import AdvancedWindow
from musicscope.comparison import IDENTICAL, Comparison
from musicscope.gui import STATUS_COLORS, MainWindow, RelativePathDelegate
from musicscope.models import Album, Inventory, Track


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(tmp_path, app):
    widget = AdvancedWindow(tmp_path / "cache")
    yield widget
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def inventory(root="/music"):
    albums = [Album(f"{root}/Artiste/Album {n}", [Track(f"{root}/Artiste/Album {n}/01.flac", "FLAC",
                    tags={"album": [f"Album {n}"], "albumartist": ["Artiste"]})]) for n in range(3)]
    return Inventory(root, albums, scanned_at="2026-11-11T10:00:00+00:00")


def navigation_rows(window):
    return [(window.navigation.item(row).text(), window.navigation.item(row).data(Qt.ItemDataRole.UserRole))
            for row in range(window.navigation.count())]


def test_sidebar_groups_every_page_by_section(window):
    rows = navigation_rows(window)
    headers = [text for text, index in rows if index is None]
    assert headers == ["BIBLIOTHÈQUE", "DOUBLONS", "SERVICES", "ORGANISATION"]
    pages = sorted(index for _, index in rows if index is not None)
    assert pages == list(range(window.tabs.count())) and window.tabs.count() == 12
    assert window.tabs.tabBar().isHidden()
    for row in range(window.navigation.count()):
        item = window.navigation.item(row)
        if item.data(Qt.ItemDataRole.UserRole) is None:
            assert not item.flags() & Qt.ItemFlag.ItemIsSelectable


def test_sidebar_and_pages_stay_in_sync(window):
    row = next(r for r in range(window.navigation.count())
               if window.navigation.item(r).data(Qt.ItemDataRole.UserRole) == window.quality_index)
    window.navigation.setCurrentRow(row)
    assert window.tabs.currentIndex() == window.quality_index
    window.tabs.setCurrentIndex(window.session_index)
    assert window.navigation.currentItem().data(Qt.ItemDataRole.UserRole) == window.session_index


def test_counts_follow_inventory(window):
    window.display_inventory(inventory())
    labels = dict((index, text) for text, index in navigation_rows(window) if index is not None)
    assert labels[0].startswith("Inventaire") and labels[0].endswith("3")
    assert labels[4] == "Erreurs de lecture"
    assert "analysé le 11/11/2026" in window.summary.text()


def test_menus_and_shortcuts(window):
    titles = [action.text() for action in window.menuBar().actions()]
    assert titles[:2] == ["Fichier", "Affichage"] and titles[-1] == "Aide"
    file_actions = {action.text(): action.shortcut().toString() for action in window.file_menu.actions()
                    if not action.isSeparator()}
    assert file_actions["Analyser"] == "F5" and file_actions["Exporter CSV…"] == "Ctrl+E"
    view = window.view_menu.actions()
    assert len(view) == window.tabs.count()
    assert view[0].shortcut().toString() == "Ctrl+1" and view[0].text() == "Bibliothèque › Inventaire"
    view[-1].trigger()
    assert window.tabs.currentIndex() == window.tags_index


def test_relative_paths_are_display_only(window):
    window.root = "/music"
    window.display_inventory(inventory())
    delegate = window.album_table.itemDelegateForColumn(6)
    assert isinstance(delegate, RelativePathDelegate)
    full = window.album_model.item(0, 6).text()
    assert full.startswith("/music/")
    assert delegate.displayText(full, QLocale()) == full[len("/music/"):]
    assert delegate.displayText("/ailleurs/x", QLocale()) == "/ailleurs/x"
    assert window.album_model.item(0, 6).toolTip() == full


def test_context_actions_depend_on_selection(window):
    one = [title for title, _, enabled in window.album_actions([object()]) if enabled]
    two = [title for title, _, enabled in window.album_actions([object(), object()]) if enabled]
    assert "Ouvrir le dossier" in one and "Comparer ces deux albums" not in one
    assert "Comparer ces deux albums" in two and "Enregistrer une décision…" in two
    assert "Vérifier avec MusicBrainz" not in two


def test_status_bar_keeps_full_message_in_tooltip(window):
    window.state.setText("Message très long " * 20)
    assert window.state.toolTip() == window.state.text()
    assert window.statusBar().isAncestorOf(window.state)
    assert window.statusBar().isAncestorOf(window.progress_bar)


def test_identical_matches_are_highlighted(window):
    album = inventory().albums[0]
    window.matches = [(album, album, Comparison(IDENTICAL))]
    window.populate_matches()
    assert window.match_model.item(0, 0).foreground().color() == QColor(STATUS_COLORS[IDENTICAL])


def test_preferences_merge_root_and_geometry(tmp_path, app):
    cache_dir = tmp_path / "cache"
    library = tmp_path / "musique"
    library.mkdir()
    first = MainWindow(cache_dir)
    try:
        first.root = str(library)
        first.save_preferences(root=first.root)
        first.resize(700, 500)  # plus petit que l’écran virtuel offscreen (800×600)
        first.close()
        data = json.loads((cache_dir / "preferences.json").read_text("utf-8"))
        assert data["root"] == str(library) and data["geometry"]
        first.save_preferences(root=str(library))
        assert "geometry" in json.loads((cache_dir / "preferences.json").read_text("utf-8"))
    finally:
        first.deleteLater()
    second = MainWindow(cache_dir)
    try:
        assert second.root == str(library)
        assert (second.width(), second.height()) == (700, 500)
    finally:
        second.close()
        second.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
