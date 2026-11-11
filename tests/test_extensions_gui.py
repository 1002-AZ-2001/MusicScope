"""Parcours avancés réels sous Qt, services externes simulés."""
import json
import os
from pathlib import Path
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QItemSelectionModel
from PySide6.QtGui import QImage, QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

from musicscope.advanced_gui import AdvancedWindow
from musicscope.demo import create_demo


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(tmp_path, qt_app):
    widget = AdvancedWindow(tmp_path / "cache")
    yield widget
    if widget.task:
        widget.task.cancel.set()
        wait(widget)
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qt_app.processEvents()


def wait(window, timeout=15):
    deadline = time.monotonic() + timeout
    idle = 0
    while time.monotonic() < deadline and idle < 3:
        QApplication.processEvents()
        QTest.qWait(5)
        idle = idle + 1 if window.task is None and window._after_task is None else 0
    assert idle >= 3, window.state.text()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def scanned(window, tmp_path):
    root = create_demo(tmp_path / "music")
    window.root = str(root)
    window.root_label.setText(str(root))
    window.start_scan()
    wait(window)
    assert len(window.inventory.albums) >= 7, window.state.text()
    return root


def select(window, *names):
    selection = window.album_table.selectionModel()
    selection.clearSelection()
    for name in names:
        index = next(i for i, album in enumerate(window.inventory.albums) if Path(album.path).name == name)
        source = window.album_model.index(index, 0)
        selection.select(window.album_proxy.mapFromSource(source),
                         QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    QApplication.processEvents()


def test_quality_decisions_cost_exact_and_session_history(window, tmp_path, monkeypatch):
    scanned(window, tmp_path)
    assert window.tabs.count() == 12
    assert window.quality_model.rowCount() > 0
    window.quality_filter.setCurrentIndex(1)
    assert window.quality_proxy.rowCount() > 0
    select(window, "Horizons FLAC", "Horizons MP3")
    window.record_decision("Conserver les deux", "Version portable et archive")
    wait(window)
    assert window.decision_model.rowCount() == 1
    assert "Inventaire concordant" in window.decision_model.item(0, 5).text()
    prompts = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: prompts.append(args[2]) or QMessageBox.StandardButton.Yes)
    window.start_exact()
    wait(window)
    assert window.exact_groups, window.state.text()
    assert any("Lecture estimée hors cache" in prompt for prompt in prompts)
    window.estimate_storage()
    wait(window)
    assert "Volume apparent" in window.storage_label.text()

    report = tmp_path / "session.json"
    window.export_review_session(report)
    wait(window)
    assert report.is_file(), window.state.text()
    payload = json.loads(report.read_text("utf-8"))
    assert payload["decisions"][0]["decision"] == "Conserver les deux"
    window.import_review_session(report)
    wait(window)
    assert window.historical
    assert "SESSION HISTORIQUE" in window.summary.text()
    assert "historique" in window.session_details.toPlainText().lower()
    assert "Historique importé" == window.decision_model.item(0, 5).text()
    assert not window.apply_action.isEnabled()
    window.compare_selected()
    assert window.task is None
    assert "actualiser" in window.state.text()
    second_report = tmp_path / "session-historique.json"
    window.export_review_session(second_report)
    wait(window)
    from musicscope.session import import_session
    reimported = import_session(second_report)
    assert reimported.original_root == str(tmp_path / "music")
    assert reimported.comparisons == window.imported_session.comparisons
    assert reimported.decisions == window.imported_session.decisions
    window.start_scan()
    wait(window)
    assert not window.historical


def test_jellyfin_separate_paths_and_credentials_never_exported(window, tmp_path, monkeypatch):
    scanned(window, tmp_path)
    from musicscope.jellyfin import Diagnosis, JellyfinFinding, PathMapping

    class Client:
        def __init__(self, url, key):
            assert url == "http://jellyfin.local:8096"
            assert key == "secret-session"

        def diagnose(self, inventory, mappings, cancel=None, progress=None):
            assert mappings[0].server_prefix == "/srv/music"
            return Diagnosis([JellyfinFinding("Sans correspondance", "/different/song.flac", "", "id-123", "Aube", "Chemin non mappé")],
                             ["Catalogue complet"], True)

    monkeypatch.setattr("musicscope.jellyfin.JellyfinClient", Client)
    window.start_jellyfin("http://jellyfin.local:8096", "secret-session", [PathMapping("/srv/music", window.root)])
    wait(window)
    assert window.jellyfin_model.rowCount() == 1
    assert window.jellyfin_model.item(0, 1).text() == "/different/song.flac"
    assert window.jellyfin_model.item(0, 2).text() == ""
    assert "Jellyfin" in {row["catégorie"] for row in window.findings()}
    assert "secret-session" not in json.dumps(window.findings())
    assert all(action.isEnabled() for action in window.extension_actions if action is not window.apply_action)


def test_recordings_aliases_and_artwork_sources(window, tmp_path, monkeypatch):
    scanned(window, tmp_path)
    from musicscope.recognition import Fingerprint, RecordingCandidate
    from musicscope.aliases import AliasResult, ArtistCandidate
    from musicscope.artwork import Artwork
    track = window.inventory.albums[0].tracks[0]

    class Printer:
        def __init__(self, path):
            pass
        def fingerprint(self, selected, cancel=None):
            assert selected is track
            return Fingerprint(1.0, "AAAA")
        def close(self):
            pass

    class AcoustID:
        def __init__(self, key, path):
            assert key == "session-key"
        def lookup(self, fingerprint):
            return [RecordingCandidate("recording", "Titre reconnu", "Artiste", 0.95, "https://musicbrainz.org/recording/recording")]
        def close(self):
            pass

    class Aliases:
        def __init__(self, path):
            pass
        def resolve(self, name):
            return AliasResult("À vérifier", [ArtistCandidate("artist-id", "東京", ["Tokyo"], "Groupe", "https://musicbrainz.org/artist/artist-id")], ["Homonymes possibles"])
        def close(self):
            pass

    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(QColor("red"))
    image_path = tmp_path / "cover.png"
    assert image.save(str(image_path))

    class Covers:
        def __init__(self, path):
            pass
        def fetch(self, release_id):
            return [Artwork("1", "https://archive.org/image.png", str(image_path), True, release_id, "same-sha")]
        def close(self):
            pass

    monkeypatch.setattr("musicscope.recognition.Fingerprinter", Printer)
    monkeypatch.setattr("musicscope.recognition.AcoustIDClient", AcoustID)
    monkeypatch.setattr("musicscope.aliases.ArtistAliasResolver", Aliases)
    monkeypatch.setattr("musicscope.artwork.ArtworkClient", Covers)
    window.start_recognition("session-key", track)
    wait(window)
    assert "Titre reconnu" in window.sources_details.toPlainText()
    window.resolve_alias("Tokyo")
    wait(window)
    assert "東京" in window.sources_details.toPlainText()
    assert "Homonymes possibles" in window.sources_details.toPlainText()
    window.fetch_artworks(["first-release", "second-release"])
    wait(window)
    assert "Images téléchargées identiques" in window.sources_details.toPlainText()
    assert window.cover_layout.count() == 2
    assert {row["catégorie"] for row in window.findings()} >= {"AcoustID", "Alias artiste", "Pochette"}


def test_confirmed_organization_and_restore_exact_paths(window, tmp_path, monkeypatch):
    scanned(window, tmp_path)
    select(window, "Horizons MP3")
    source = Path(window.selected_albums()[0].path)
    before = {p.name: p.read_bytes() for p in source.iterdir()}
    destination = tmp_path / "organized"
    destination.mkdir()
    window.preview_organization(destination)
    wait(window)
    plan = window.pending_plan
    assert plan is not None and not plan.blockers, window.state.text()
    assert source.exists() and not Path(plan.destination).exists()
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)
    window.apply_organization()
    assert source.exists() and window.task is None
    prompts = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: prompts.append(args[2]) or QMessageBox.StandardButton.Yes)
    window.apply_organization()
    wait(window)
    assert not source.exists()
    assert Path(plan.destination).is_dir(), window.state.text()
    assert window.requires_rescan
    assert window.pending_plan is None
    assert not window.apply_action.isEnabled()
    journal = next((window.cache_dir / "journals").glob("*.json"))
    window.restore_organization(journal)
    wait(window)
    assert source.is_dir(), window.state.text()
    assert {p.name: p.read_bytes() for p in source.iterdir()} == before
    assert any(str(source) in prompt and str(plan.destination) in prompt for prompt in prompts)
    assert window.requires_rescan


def test_invalid_restore_preview_never_invokes_engine(window, tmp_path, monkeypatch):
    path = tmp_path / "invalid.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(QMessageBox, "question", lambda *args: pytest.fail("Pas de confirmation pour journal invalide"))
    window.restore_organization(path)
    assert window.task is None
    assert "Journal illisible" in window.state.text()
