"""Icône de fenêtre, menu Aide et fenêtre « À propos » sans écran ni réseau."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication

from musicscope import __version__
from musicscope.advanced_gui import AdvancedWindow
from musicscope.cache import Cache
from musicscope.gui import ISSUES_URL, AboutDialog, MainWindow, about_html, app_icon
from musicscope.models import Album, Inventory, Track


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


def close(widget, app):
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_icon_is_embedded_and_set_on_window(tmp_path, app):
    assert not app_icon().isNull()
    assert app_icon().pixmap(64, 64).width() == 64
    window = MainWindow(tmp_path / "cache")
    try:
        assert not window.windowIcon().isNull()
    finally:
        close(window, app)


def test_help_menu_is_last_and_complete(tmp_path, app):
    window = AdvancedWindow(tmp_path / "cache")
    try:
        titles = [action.text() for action in window.menuBar().actions()]
        assert titles == ["Fichier", "Affichage", "Services", "Revue", "Session", "Rangement", "Aide"]
        entries = [action.text() for action in window.help_menu.actions() if not action.isSeparator()]
        assert entries == ["Documentation en ligne", "Signaler un problème…", "Ouvrir le dossier du cache",
                           "À propos de MusicScope", "À propos de Qt"]
        about = next(action for action in window.help_menu.actions() if action.text() == "À propos de MusicScope")
        assert not about.shortcut().isEmpty()
    finally:
        close(window, app)


def test_help_links_open_without_network_access(tmp_path, app, monkeypatch):
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toString()) or True)
    window = MainWindow(tmp_path / "cache")
    try:
        actions = {action.text(): action for action in window.help_menu.actions()}
        actions["Signaler un problème…"].trigger()
        actions["Ouvrir le dossier du cache"].trigger()
        assert opened[0] == ISSUES_URL
        assert opened[1].startswith("file://") and opened[1].endswith("cache")
    finally:
        close(window, app)


def test_about_reports_version_components_and_cache(tmp_path, app):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    with Cache(cache_dir / "inventory.sqlite3") as cache:
        cache.put_track(Track("/m/a.flac", size=1, mtime_ns=1))
        cache.save_inventory(Inventory("/m", [Album("/m", [Track("/m/a.flac")])], scanned_at="2026-11-09T20:00:00+00:00"))
    text = about_html(cache_dir)
    assert f"MusicScope {__version__}" in text and "AGPLv3" in text
    assert "1 fichiers aux tags mémorisés" in text and "1 inventaire(s)" in text
    assert "PySide6" in text and "Mutagen" in text
    dialog = AboutDialog(cache_dir)
    try:
        assert __version__ in dialog.text.toPlainText()
        assert dialog.text.openExternalLinks()
    finally:
        close(dialog, app)


def test_startup_message_says_inventory_comes_from_cache(tmp_path, app):
    import json
    library = tmp_path / "musique"
    library.mkdir()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / "preferences.json").write_text(json.dumps({"root": str(library)}), "utf-8")
    with Cache(cache_dir / "inventory.sqlite3") as cache:
        cache.save_inventory(Inventory(str(library), [Album(str(library / "A"), [Track(str(library / "A" / "1.flac"))])],
                                       scanned_at="2026-11-09T20:00:00+00:00"))
    window = MainWindow(cache_dir)
    try:
        message = window.state.text()
        assert "09/11/2026" in message and "1 albums" in message and "seuls les fichiers modifiés" in message
    finally:
        close(window, app)
