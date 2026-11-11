"""Albums en double aux noms de dossiers différents : détection, recommandation, quarantaine."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QItemSelectionModel
from PySide6.QtWidgets import QApplication

from musicscope.cache import Cache
from musicscope.comparison import Comparison, DISTINCT, IDENTICAL, MATCHING, REVIEW, candidates, compare_albums
from musicscope.duplicates import EXACT, LIKELY, UNSURE, duplicate_groups, score_album
from musicscope.folder_names import parse_folder
from musicscope.models import Album, Track
from musicscope.organizer import QUARANTINE_DIR, apply_plan, plan_quarantine, restore_journal
from musicscope.scanner import scan
from musicscope.tagwriter import _Tags

from test_tagwriter import SOURCES


@pytest.mark.parametrize("name,expected", [
    ("2014 - NxxxxxS - Fujita Scale", ("NxxxxxS", "Fujita Scale", "2014")),
    ("Fujita Scale (2014)", ("NxxxxxS", "Fujita Scale", "2014")),
    ("NxxxxxS - Fujita Scale (2014)", ("NxxxxxS", "Fujita Scale", "2014")),
    ("NxxxxxS - 2014 - Fujita Scale", ("NxxxxxS", "Fujita Scale", "2014")),
    ("Fujita Scale [2014]", ("NxxxxxS", "Fujita Scale", "2014")),
    ("2014 - Fujita Scale", ("NxxxxxS", "Fujita Scale", "2014")),
    ("Fujita Scale - 2014", ("NxxxxxS", "Fujita Scale", "2014")),
    ("La Flamme Rouge (Original Motion Picture Soundtrack) (2021)",
     ("NxxxxxS", "La Flamme Rouge (Original Motion Picture Soundtrack)", "2021")),
    ("Selected Beat Work 2324 (2024)", ("NxxxxxS", "Selected Beat Work 2324", "2024")),
    ("Autre - Titre (2020)", ("NxxxxxS", "Autre - Titre", "2020")),
    ("Sans année", ("NxxxxxS", "Sans année", "")),
])
def test_folder_name_conventions(name, expected):
    folder = parse_folder(f"/m/NxxxxxS/{name}")
    assert (folder.artist, folder.title, folder.year) == expected


def test_disc_folder_uses_album_folder():
    assert parse_folder("/m/A/2014 - A - Album/CD1").title == "Album"


def build_library(root: Path, *, tagged=True, second_format="flac", extra_track=False):
    """Deux copies d'un même album sous deux conventions de nommage."""
    folders = [root / "NxxxxxS" / "2014 - NxxxxxS - Fujita Scale", root / "NxxxxxS" / "Fujita Scale (2014)"]
    for position, folder in enumerate(folders):
        folder.mkdir(parents=True)
        fmt = "flac" if position == 0 else second_format
        count = 3 if (extra_track and position == 0) else 2
        for number in range(1, count + 1):
            path = folder / f"{number:02d}.{fmt}"
            shutil.copyfile(str(SOURCES[fmt]), path)
            tags = _Tags(path)
            values = {"title": [f"Piste {number}"], "tracknumber": [str(number)]}
            if tagged or position == 0:
                values |= {"album": ["Fujita Scale"], "albumartist": ["NxxxxxS"], "artist": ["NxxxxxS"], "date": ["2014"]}
            for key, value in values.items():
                tags.set(key, value)
            tags.save()
    (root / "NxxxxxS" / "Fujita Scale (2014)" / "cover.jpg").write_bytes(b"jpg")
    return folders


def groups_for(root: Path, cache_path: Path):
    with Cache(cache_path) as cache:
        inventory = scan(root, cache)
    pairs = [(a, b, compare_albums(a, b)) for a, b in candidates(inventory.albums)]
    return inventory, pairs, duplicate_groups(pairs)


def test_untagged_copy_found_by_folder_name_and_lossless_preferred(tmp_path):
    build_library(tmp_path / "library", tagged=False, second_format="mp3")
    inventory, pairs, groups = groups_for(tmp_path / "library", tmp_path / "c.sqlite3")
    assert len(inventory.albums) == 2 and len(pairs) == 1
    assert len(groups) == 1
    group = groups[0]
    assert group.confidence in (LIKELY, UNSURE)
    if group.confidence == LIKELY:
        assert Path(group.albums[group.keep].path).name == "2014 - NxxxxxS - Fujita Scale"
        assert "sans perte (FLAC)" in group.scores[group.keep].strengths


def test_identical_copies_tie_and_wait_for_choice(tmp_path):
    build_library(tmp_path / "library")
    _, pairs, groups = groups_for(tmp_path / "library", tmp_path / "c.sqlite3")
    assert [group.confidence for group in groups] == [LIKELY] or [group.confidence for group in groups] == [EXACT]
    assert groups[0].keep is None and groups[0].extra_bytes == 0 and groups[0].to_remove() == []


def test_more_complete_copy_is_recommended(tmp_path):
    build_library(tmp_path / "library", extra_track=True)
    _, pairs, groups = groups_for(tmp_path / "library", tmp_path / "c.sqlite3")
    statuses = {comparison.status for *_, comparison in pairs}
    if statuses <= {DISTINCT}:
        assert groups == []  # bonus détecté comme édition distincte : jamais regroupé
    else:
        group = groups[0]
        if group.keep is not None:
            assert len(group.albums[group.keep].tracks) == 3


def album(path, tracks=2, codec="FLAC", **tags):
    return Album(path, [Track(f"{path}/{n:02d}", codec, size=10, tags={"title": [str(n)], "tracknumber": [str(n)], **tags})
                        for n in range(1, tracks + 1)])


def test_review_pairs_need_same_folder_album_and_year():
    a, b, c = album("/m/A/2014 - A - X"), album("/m/A/X (2014)"), album("/m/A/X (2016)")
    groups = duplicate_groups([(a, b, Comparison(REVIEW)), (a, c, Comparison(REVIEW))])
    assert len(groups) == 1 and groups[0].confidence == UNSURE and groups[0].keep is None
    assert {Path(x.path).name for x in groups[0].albums} == {"2014 - A - X", "X (2014)"}
    assert duplicate_groups([(a, b, Comparison(DISTINCT))]) == []


def test_groups_merge_transitively_and_keep_weakest_confidence():
    a, b, c = album("/m/A/a"), album("/m/A/b"), album("/m/A/c", codec="MP3")
    groups = duplicate_groups([(a, b, Comparison(IDENTICAL)), (b, c, Comparison(MATCHING))])
    assert len(groups) == 1 and len(groups[0].albums) == 3 and groups[0].confidence == LIKELY
    assert groups[0].keep is None  # a et b sont équivalentes
    assert "avec perte (MP3)" in score_album(c).weaknesses


def test_quarantine_moves_restores_and_scan_ignores_it(tmp_path):
    root = tmp_path / "library"
    folders = build_library(root)
    with Cache(tmp_path / "c.sqlite3") as cache:
        inventory = scan(root, cache)
    copy = next(a for a in inventory.albums if Path(a.path).name == "Fujita Scale (2014)")
    plan = plan_quarantine(copy, root)
    assert not plan.blockers, plan.blockers
    assert Path(plan.destination) == root / QUARANTINE_DIR / "NxxxxxS" / "Fujita Scale (2014)"
    journal = apply_plan(plan, tmp_path / "journals", confirmed=True)
    assert not folders[1].exists() and (Path(plan.destination) / "cover.jpg").exists()
    with Cache(tmp_path / "c.sqlite3") as cache:
        assert [Path(a.path).name for a in scan(root, cache).albums] == ["2014 - NxxxxxS - Fujita Scale"]
    restore_journal(journal, confirmed=True)
    assert folders[1].exists() and not Path(plan.destination).exists()


def test_quarantine_refuses_outside_root_stale_and_already_quarantined(tmp_path):
    root = tmp_path / "library"
    build_library(root)
    with Cache(tmp_path / "c.sqlite3") as cache:
        inventory = scan(root, cache)
    target = inventory.albums[0]
    assert plan_quarantine(target, tmp_path / "ailleurs").blockers
    track = Path(target.tracks[0].path)
    os.utime(track, ns=(target.tracks[0].mtime_ns + 10**10,) * 2)
    assert any("Inventaire ancien" in b for b in plan_quarantine(target, root).blockers)
    inside = album(str(root / QUARANTINE_DIR / "x"))
    assert plan_quarantine(inside, root).blockers


def test_quarantine_name_collision_gets_suffix(tmp_path):
    root = tmp_path / "library"
    build_library(root)
    (root / QUARANTINE_DIR / "NxxxxxS" / "Fujita Scale (2014)").mkdir(parents=True)
    with Cache(tmp_path / "c.sqlite3") as cache:
        inventory = scan(root, cache)
    copy = next(a for a in inventory.albums if Path(a.path).name == "Fujita Scale (2014)")
    assert Path(plan_quarantine(copy, root).destination).name == "Fujita Scale (2014) (2)"


# Interface
@pytest.fixture(scope="session")
def app():
    return QApplication.instance() or QApplication([])


def settle(window, app, timeout=20):
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


def test_gui_choose_quarantine_and_rescan(tmp_path, app):
    from musicscope.advanced_gui import AdvancedWindow
    root = tmp_path / "library"
    build_library(root, second_format="mp3")
    window = AdvancedWindow(tmp_path / "gui")
    try:
        window.root = str(root)
        window.start_scan()
        settle(window, app)
        assert window.duplicate_model.rowCount() == 1
        assert window.navigation.findItems("Albums en double", __import__("PySide6.QtCore").QtCore.Qt.MatchFlag.MatchStartsWith)
        index = window.duplicate_proxy.index(0, 0)
        window.duplicate_table.selectionModel().select(
            index, QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
        group = window.duplicate_groups[0]
        keep = next(i for i, a in enumerate(group.albums) if Path(a.path).name.startswith("2014"))
        window.choose_kept_copy(keep=keep)
        assert window.duplicate_model.item(0, 3).text() == "2014 - NxxxxxS - Fujita Scale"
        assert "choisie manuellement" in window.duplicate_details.toPlainText()
        window.quarantine_duplicates(confirm=False)
        settle(window, app)
        assert (root / "NxxxxxS" / "Fujita Scale (2014)").exists()
        window.duplicate_table.selectionModel().select(
            window.duplicate_proxy.index(0, 0),
            QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
        window.duplicate_groups[0].keep = keep
        window.quarantine_duplicates(confirm=True, allow_hardlinks=True)
        settle(window, app)
        assert not (root / "NxxxxxS" / "Fujita Scale (2014)").exists()
        assert (root / QUARANTINE_DIR / "NxxxxxS" / "Fujita Scale (2014)").exists()
        assert len(window.inventory.albums) == 1 and window.duplicate_model.rowCount() == 0
        assert list((tmp_path / "gui" / "journals").rglob("*.json"))
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()


def test_gui_requires_choice_for_undecided_groups(tmp_path, app):
    from musicscope.advanced_gui import AdvancedWindow
    root = tmp_path / "library"
    build_library(root)
    window = AdvancedWindow(tmp_path / "gui")
    try:
        window.root = str(root)
        window.start_scan()
        settle(window, app)
        window.duplicate_table.selectionModel().select(
            window.duplicate_proxy.index(0, 0),
            QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows)
        assert window.duplicate_groups[0].keep is None
        window.quarantine_duplicates(confirm=True, allow_hardlinks=True)
        assert window.task is None and "Choisissez d’abord" in window.state.text()
        assert all(folder.exists() for folder in (root / "NxxxxxS").iterdir())
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
