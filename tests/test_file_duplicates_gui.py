"""Page « Fichiers en double » : recherche, aides de sélection, garde-fou, quarantaine, restauration, vidage."""
import os
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication

from musicscope import file_duplicates as fd
from musicscope.advanced_gui import AdvancedWindow
from musicscope.organizer import QUARANTINE_DIR

from test_file_duplicates import audio, write


@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


def settle(window, app, timeout=30):
    deadline, idle = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        app.processEvents()
        if window.task is None:
            idle = idle or time.monotonic()
            if time.monotonic() - idle > 0.3:
                return
        else:
            idle = None
        time.sleep(0.01)
    raise AssertionError("tâche non terminée")


@pytest.fixture
def setup(tmp_path, app):
    root = tmp_path / "lib"
    write(root / "A" / "cover.jpg", b"image-1", 1_600_000_000)
    write(root / "B" / "cover.jpg", b"image-1", 1_700_000_000)
    write(root / "C" / "front.jpg", b"image-1", 1_650_000_000)
    audio(root / "2013 - X - Hydro" / "01.flac", "flac")
    audio(root / "Hydro (2013)" / "01.mp3", "mp3")
    window = AdvancedWindow(tmp_path / "gui")
    window.root = str(root)
    page = window.file_duplicates_page
    page.min_size.setValue(0)  # fichiers de test de quelques octets
    yield window, page, root, app
    if window.task:
        window.task.cancel.set()
        settle(window, app)
    window.close()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    app.processEvents()


def test_page_in_sidebar_with_default_content_criterion(setup):
    window, page, _, _ = setup
    assert window.tabs.widget(window.file_duplicates_index) is page
    checked = [name for name, box in page.criteria_boxes.items() if box.isChecked()]
    assert checked == [fd.CONTENT]
    assert all(box.toolTip() for box in page.criteria_boxes.values())


def test_search_select_quarantine_restore_and_empty(setup):
    window, page, root, app = setup
    page.search()
    settle(window, app)
    assert len(page.groups) == 1 and page.tree.topLevelItem(0).childCount() == 3
    assert "3 fichiers" in page.tree.topLevelItem(0).text(0)
    page.apply_selection(set(p for g in page.groups for r in g.files for p in [r.path]))
    assert "entièrement coché" in page.summary.text()
    page.quarantine(confirm=True)
    assert window.task is None and "Refusé" in window.state.text()
    page.apply_selection(fd.select_keep_by_date(page.groups, newest=False))
    assert "2 fichier(s) coché(s)" in page.summary.text()
    page.quarantine(confirm=False)
    assert (root / "B" / "cover.jpg").exists()
    page.quarantine(confirm=True)
    settle(window, app)
    assert (root / "A" / "cover.jpg").exists()
    assert not (root / "B" / "cover.jpg").exists() and (root / QUARANTINE_DIR / "B" / "cover.jpg").exists()
    assert page.tree.topLevelItemCount() == 0 and window.requires_rescan
    journal = next((window.cache_dir / "journals").glob("*-fichiers-*.json"))
    page.restore(str(journal))
    settle(window, app)
    assert (root / "B" / "cover.jpg").exists() and (root / "C" / "front.jpg").exists()
    page.search()
    settle(window, app)
    page.apply_selection(fd.select_all_but_best(page.groups))
    page.quarantine(confirm=True)
    settle(window, app)
    page.empty(confirm=False)
    settle(window, app)  # mesure de la quarantaine en tâche de fond
    assert fd.quarantine_summary(root)[1] == 2
    page.empty(confirm=True)
    settle(window, app)
    assert not (root / QUARANTINE_DIR).exists() and "vidée" in window.state.text()


def test_song_criterion_and_lossy_helper(setup):
    window, page, root, app = setup
    page.criteria_boxes[fd.CONTENT].setChecked(False)
    page.criteria_boxes[fd.SONG].setChecked(True)
    page.search()
    settle(window, app)
    assert len(page.groups) == 1
    page.apply_selection(fd.select_lossy_when_lossless(page.groups))
    assert page.selected() == {str(root / "Hydro (2013)" / "01.mp3")}
    best = page.items[str(root / "2013 - X - Hydro" / "01.flac")]
    assert "Meilleure copie" in best.toolTip(0)


def test_folder_selection_and_filters(setup):
    window, page, root, app = setup
    page.include.setText("jpg")
    page.folders.setText("C")
    page.search()
    settle(window, app)
    assert {Path(r.path).parent.name for g in page.groups for r in g.files} == {"A", "B"}
    page.set_folder(str(root / "B"), True)
    assert page.selected() == {str(root / "B" / "cover.jpg")}
    page.set_folder(str(root / "B"), False)
    assert page.selected() == set()


def test_no_criterion_and_missing_fpcalc_are_reported(setup, monkeypatch):
    window, page, _, app = setup
    for box in page.criteria_boxes.values():
        box.setChecked(False)
    page.search()
    assert window.task is None and "critère" in window.state.text()
    page.criteria_boxes[fd.FINGERPRINT].setChecked(True)
    monkeypatch.setattr("shutil.which", lambda name: None)
    page.search()
    settle(window, app)
    assert "Chromaprint" in window.state.text() or "fpcalc" in window.state.text()
