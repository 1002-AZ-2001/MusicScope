"""Parcours Qt réel sans écran ni réseau : scan, sélection, comparaison, export."""
import csv
import hashlib
import os
import time
from pathlib import Path

# L'import de Qt doit suivre le choix du backend, y compris en CI sans écran.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QItemSelectionModel, QThread, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QFileDialog

from musicscope.demo import create_demo
from musicscope.gui import MainWindow
from musicscope.musicbrainz import ReleaseCandidate, TagProposal, Verification


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def window(tmp_path, app):
    widget = MainWindow(tmp_path / "cache")
    yield widget
    if widget.task:
        widget.task.cancel.set()
        wait_for_task(widget)
    widget.close()
    widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def wait_for_task(window, timeout=10):
    deadline = time.monotonic() + timeout
    while window.task is not None and time.monotonic() < deadline:
        QApplication.processEvents()
        QTest.qWait(5)
    assert window.task is None, f"Le worker ne termine pas : {window.state.text()}"
    QApplication.processEvents()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def select_albums(window, *folder_names):
    selection = window.album_table.selectionModel()
    selection.clearSelection()
    for name in folder_names:
        album_index = next(i for i, a in enumerate(window.inventory.albums) if Path(a.path).name == name)
        source_index = window.album_model.index(album_index, 0)
        proxy_index = window.album_proxy.mapFromSource(source_index)
        assert proxy_index.isValid()
        selection.select(proxy_index, QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows)
    QApplication.processEvents()


def fingerprints(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def test_full_qt_scan_compare_exact_export_and_restore(window, tmp_path, monkeypatch):
    library = create_demo(tmp_path / "musique")
    before = fingerprints(library)
    window.root = str(library)
    window.root_label.setText(str(library))
    window.start_scan()
    wait_for_task(window)
    assert window.album_model.rowCount() >= 7, window.state.text()
    assert window.match_model.rowCount() > 0
    assert "Analyse terminée" in window.state.text()
    assert any(Path(a.path).name == "Concert" and len(a.tracks) == 2 for a in window.inventory.albums)

    # Une sélection à travers le proxy filtré/trié doit garder la bonne identité.
    window.album_table.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    window.filter.setText("Horizons MP3")
    QApplication.processEvents()
    assert window.album_proxy.rowCount() == 1
    window.album_table.selectRow(0)
    assert Path(window.selected_albums()[0].path).name == "Horizons MP3"
    window.filter.clear()
    QApplication.processEvents()

    select_albums(window, "Horizons FLAC", "Copie noms différents")
    assert len(window.selected_albums()) == 2
    window.compare_selected()
    wait_for_task(window)
    assert window.matches[-1][2].status == "Fichiers identiques octet pour octet", window.state.text()
    assert "Aube" in window.left_details.toPlainText()
    assert "Aube" in window.right_details.toPlainText()

    window.start_exact()
    wait_for_task(window)
    assert window.exact_groups
    assert any(any("copie-1.flac" in t.path for t in group) for group in window.exact_groups)
    assert "identiques octet pour octet" in window.exact_details.toPlainText()
    report = tmp_path / "rapport.csv"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(report), "CSV (*.csv)"))
    window.export()
    with report.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter=";"))
    assert {row["catégorie"] for row in rows} >= {"Inventaire", "Audit local", "Comparaison", "Fichiers identiques"}
    assert any(row["statut"] == "Fichiers identiques octet pour octet" for row in rows)
    assert fingerprints(library) == before

    restored = MainWindow(window.cache_dir)
    try:
        assert restored.root == str(library)
        assert restored.album_model.rowCount() == window.album_model.rowCount()
        assert "chargé depuis le cache" in restored.state.text()
    finally:
        restored.close()
        restored.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


def test_gui_musicbrainz_result_and_open_folder_with_mocked_service(window, tmp_path, monkeypatch):
    library = create_demo(tmp_path / "musique")
    window.root = str(library)
    window.start_scan()
    wait_for_task(window)
    select_albums(window, "Horizons FLAC")
    album = window.selected_albums()[0]
    url = "https://musicbrainz.org/release/11111111-1111-4111-8111-111111111111"

    class FakeClient:
        def __init__(self, path):
            assert path == window.cache_dir / "musicbrainz.sqlite3"

        def verify_album(self, selected):
            assert selected.path == album.path
            return Verification("Édition probable", [ReleaseCandidate("test", "Horizons", url=url, plausible=True)],
                                [TagProposal(selected.tracks[0].path, "date", "2024", "2023", "Date d'édition", url)],
                                ["Une édition cohérente"])

        def close(self):
            pass

    monkeypatch.setattr("musicscope.gui.MusicBrainzClient", FakeClient)
    window.start_musicbrainz()
    wait_for_task(window)
    text = window.mb_details.toPlainText()
    assert "Édition probable" in text
    assert "2024" in text and "2023" in text and "Date d'édition" in text
    assert {row["catégorie"] for row in window.findings()} >= {"MusicBrainz", "Édition candidate", "Proposition"}
    opened = []
    monkeypatch.setattr("musicscope.gui.QDesktopServices.openUrl", lambda url: opened.append(url.toLocalFile()) or True)
    window.open_folder()
    assert opened == [album.path]


def test_unavailable_rescan_keeps_gui_inventory(window, tmp_path):
    library = create_demo(tmp_path / "musique")
    window.root = str(library)
    window.start_scan()
    wait_for_task(window)
    previous = window.inventory
    library.rename(tmp_path / "partage_deconnecte")
    window.start_scan()
    wait_for_task(window)
    assert window.inventory is previous
    assert "ScanUnavailable" in window.state.text()
    assert window.album_model.rowCount() >= 7


def test_cancel_updates_controls_and_preserves_inventory(window):
    previous = window.inventory

    def operation(cancel, progress):
        from musicscope.cache import check_cancel
        cancel.wait(2)
        check_cancel(cancel)

    window.run_task("Tâche annulable", operation, lambda result: pytest.fail("Résultat inattendu"))
    assert window.cancel_button.isEnabled()
    window.cancel_task()
    wait_for_task(window)
    assert "annulée" in window.state.text()
    assert window.inventory is previous
    assert window.scan_button.isEnabled()
    assert not window.cancel_button.isEnabled()


def test_worker_results_are_delivered_on_qt_main_thread(window, app):
    observed = {}

    def operation(cancel, progress):
        observed["worker"] = QThread.currentThread()
        progress(1, 1, "Terminé")
        return "résultat"

    def done(result):
        observed["callback"] = QThread.currentThread()
        observed["result"] = result

    window.run_task("Traitement", operation, done)
    wait_for_task(window)
    assert observed["worker"] != app.thread()
    assert observed["callback"] == app.thread()
    assert observed["result"] == "résultat"
