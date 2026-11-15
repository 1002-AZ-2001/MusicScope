"""Interface Jellyfin : test de connexion, bibliothèques, réglages mémorisés sans clé, filtre, échec lisible."""
import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from musicscope.advanced_gui import AdvancedWindow, JellyfinDialog
from musicscope.jellyfin import Diagnosis, JellyfinFinding, JellyfinUnavailable, Library, PathMapping, ServerInfo
from musicscope.models import Album, Inventory, Track


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


class FakeClient:
    fail = None

    def __init__(self, url, key, library_id=""):
        self.url, self.key, self.library_id = url, key, library_id

    def server_info(self):
        if self.fail:
            raise JellyfinUnavailable(self.fail)
        return ServerInfo("TrueNAS", "12.0.1")

    def libraries(self):
        return [Library("Musique", "abc-123", "music", ("/media/Musique",)),
                Library("Films", "f1", "movies", ("/media/Films",))]


def test_dialog_tests_connection_lists_music_and_prefills_mapping(tmp_path, app):
    dialog = JellyfinDialog(str(tmp_path), {"server": "http://nas:8096", "key": "k"}, client_factory=FakeClient)
    try:
        assert dialog.test_connection()
        assert "TrueNAS" in dialog.status.text() and "12.0.1" in dialog.status.text()
        assert dialog.library.count() == 2 and dialog.library.currentData() == "abc-123"
        server, key, rows, library_id = dialog.configuration()
        assert (server, key, library_id) == ("http://nas:8096", "k", "abc-123")
        assert rows == [("/media/Musique", str(tmp_path))]
        assert dialog.library_name().startswith("Musique")
    finally:
        dialog.deleteLater()


def test_dialog_keeps_user_mappings_and_reports_failure(tmp_path, app):
    dialog = JellyfinDialog(str(tmp_path), {"mappings": [("/srv/zik", "/mnt/nas/zik")]}, client_factory=FakeClient)
    try:
        dialog.test_connection()
        assert dialog.configuration()[2] == [("/srv/zik", "/mnt/nas/zik")]
        FakeClient.fail = "Clé API refusée (HTTP 401)."
        assert not dialog.test_connection()
        assert "HTTP 401" in dialog.status.text()
    finally:
        FakeClient.fail = None
        dialog.deleteLater()


@pytest.fixture
def window(tmp_path, app):
    widget = AdvancedWindow(tmp_path / "cache")
    root = tmp_path / "music"
    root.mkdir()
    widget.root = str(root)
    widget.display_inventory(Inventory(str(root), [Album(str(root / "A"), [Track(str(root / "A" / "1.flac"))])],
                                       scanned_at="2026-11-15T10:00:00+00:00"))
    yield widget
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def settle(window, app):
    deadline = time.monotonic() + 10
    while window.task is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()


def run_with(window, app, monkeypatch, diagnosis):
    class Client:
        def __init__(self, url, key, library_id=""):
            pass

        def diagnose(self, inventory, mappings, cancel=None, progress=None):
            return diagnosis

    monkeypatch.setattr("musicscope.jellyfin.JellyfinClient", Client)
    window.start_jellyfin("http://nas:8096", "secret", [PathMapping("/m", window.root)])
    settle(window, app)


def test_failure_shows_reason_without_flooding_rows(window, app, monkeypatch):
    run_with(window, app, monkeypatch, Diagnosis(reasons=["Clé API refusée (HTTP 401)."], failed=True))
    assert window.jellyfin_model.rowCount() == 0
    assert "HTTP 401" in window.jellyfin_summary.text() and "HTTP 401" in window.state.text()


def test_problem_filter_counts_and_summary(window, app, monkeypatch):
    findings = [JellyfinFinding("Correspondance", "/m/a", "/l/a"), JellyfinFinding("Correspondance", "/m/b", "/l/b"),
                JellyfinFinding("Fichier local absent", "/m/c", "/l/c")]
    run_with(window, app, monkeypatch, Diagnosis(findings, ["ok"], True, undetermined=0))
    assert window.jellyfin_model.rowCount() == 3
    assert window.jellyfin_proxy.rowCount() == 1
    assert "1 fichier local absent" in window.jellyfin_summary.text().lower()
    item = next(window.navigation.item(r) for r in range(window.navigation.count())
                if window.navigation.item(r).data(0x0100) == window.jellyfin_index)
    assert item.text().endswith("1")
    window.jellyfin_filter.setCurrentIndex(1)
    assert window.jellyfin_proxy.rowCount() == 3


def test_preferences_store_settings_but_never_the_key(window, app, monkeypatch):
    class Dialog:
        def __init__(self, *args, **kwargs):
            pass

        def exec(self):
            from PySide6.QtWidgets import QDialog
            return QDialog.DialogCode.Accepted

        def configuration(self):
            return "http://nas:8096", "cle-secrete", [("/media/Musique", window.root)], "abc-123"

        def library_name(self):
            return "Musique"

    monkeypatch.setattr("musicscope.advanced_gui.JellyfinDialog", Dialog)
    run_with_dialog = Diagnosis([], ["ok"], True)

    class Client:
        def __init__(self, url, key, library_id=""):
            assert library_id == "abc-123"

        def diagnose(self, *args, **kwargs):
            return run_with_dialog

    monkeypatch.setattr("musicscope.jellyfin.JellyfinClient", Client)
    window.start_jellyfin()
    settle(window, app)
    saved = json.loads((window.cache_dir / "preferences.json").read_text("utf-8"))
    assert saved["jellyfin"]["server"] == "http://nas:8096" and saved["jellyfin"]["library_id"] == "abc-123"
    assert "cle-secrete" not in json.dumps(saved)
    assert window._jellyfin_previous()["key"] == "cle-secrete"  # session uniquement
