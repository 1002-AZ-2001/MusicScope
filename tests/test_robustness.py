"""Robustesse (v0.10.2) : régressions issues de l'audit.

Un fichier ou une base abîmés ne doivent ni bloquer le démarrage, ni annuler tout un
scan ; l'interface ne doit ni perdre un choix de l'utilisateur, ni accepter une action
qui contredit la tâche en cours.
"""
import os
import shutil
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMessageBox

import musicscope
from musicscope import file_duplicates as fd
from musicscope.cache import Cache
from musicscope.metadata import canonical_tags, read_track
from musicscope.models import Album, Inventory, Track
from musicscope.scanner import scan

TONE = Path(musicscope.__file__).parent / "data" / "tone-a.flac"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


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


# Lecture des fichiers
def test_unexpected_decoder_error_marks_only_that_file(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    (root / "Album").mkdir(parents=True)
    shutil.copy(TONE, root / "Album" / "01.flac")
    shutil.copy(TONE, root / "Album" / "02.flac")
    import mutagen
    original = mutagen.File

    def fragile(path, *args, **kwargs):
        if Path(path).name == "02.flac":
            raise IndexError("bytearray index out of range")  # .ogg tronqué, cf. mutagen/_vorbis.py
        return original(path, *args, **kwargs)

    monkeypatch.setattr("musicscope.metadata.mutagen.File", fragile)
    with Cache(tmp_path / "cache.sqlite3") as cache:
        inventory = scan(root, cache, workers=1)
    [album] = inventory.albums
    assert [Path(track.path).name for track in album.tracks] == ["01.flac"]
    assert any("02.flac" in error and "corrompu" in error for error in inventory.errors)
    with pytest.raises(ValueError, match="IndexError"):
        read_track(root / "Album" / "02.flac")


def test_empty_mp4_track_atom_is_ignored():
    assert canonical_tags({"trkn": [], "disk": [(1, 2)], "©nam": ["Titre"]}) == {
        "discnumber": ["1"], "disctotal": ["2"], "title": ["Titre"]}


@pytest.mark.skipif(os.name == "nt", reason="noms d'octets bruts propres à POSIX")
def test_non_utf8_file_name_is_reported_without_aborting(tmp_path):
    root = tmp_path / "lib"
    (root / "Album").mkdir(parents=True)
    shutil.copy(TONE, root / "Album" / "01 ok.flac")
    shutil.copy(TONE, os.fsdecode(os.fsencode(str(root / "Album")) + b"/02 caf\xe9.flac"))
    with Cache(tmp_path / "cache.sqlite3") as cache:
        inventory = scan(root, cache, workers=1)
        assert [Path(track.path).name for track in inventory.albums[0].tracks] == ["01 ok.flac"]
        assert any("non UTF-8" in error for error in inventory.errors)
        assert cache.load_inventory(root) is not None


def test_inventory_from_another_version_does_not_block_scans(tmp_path):
    root = tmp_path / "lib"
    (root / "Album").mkdir(parents=True)
    shutil.copy(TONE, root / "Album" / "01.flac")
    with Cache(tmp_path / "cache.sqlite3") as cache:
        scan(root, cache, workers=1)
        body = cache.connection.execute("SELECT body FROM inventories").fetchone()[0]
        cache.connection.execute("UPDATE inventories SET body=?", (body.replace('"codec"', '"bitrate": 320, "codec"', 1),))
        cache.connection.commit()
        assert cache.load_inventory(root).albums[0].tracks  # champ inconnu ignoré
        cache.connection.execute("UPDATE inventories SET body='{abîmé'")
        cache.connection.commit()
        assert cache.load_inventory(root) is None
        assert scan(root, cache, workers=1).albums


def test_track_from_dict_ignores_unknown_fields():
    track = Track.from_dict({"path": "/a.flac", "size": 3, "bitrate": 320})
    assert track.path == "/a.flac" and track.size == 3


# Fichiers en double
def test_fingerprint_failure_on_one_file_does_not_stop_the_search(tmp_path):
    from musicscope.recognition import FeatureUnavailable
    root = tmp_path / "lib"
    paths = []
    for name in ("a", "b", "c"):
        path = root / name / "01.flac"
        path.parent.mkdir(parents=True)
        shutil.copy(TONE, path)
        paths.append(path)

    class Printer:
        def raw(self, track, cancel=None):
            if track.path.endswith("c/01.flac"):
                raise FeatureUnavailable("Réponse Chromaprint illisible.")
            return 10.0, list(range(100))

    tracks = {str(path): read_track(path) for path in paths}
    options = fd.ScanOptions(frozenset({fd.FINGERPRINT}), min_size=1)
    records = fd.collect_files(root, options, tracks)
    groups = fd.find_duplicates(records, options, None, Printer())
    assert [sorted(Path(record.path).parent.name for record in group.files) for group in groups] == [["a", "b"]]


def test_unknown_inode_never_merges_distinct_files():
    records = [fd.FileRecord(f"/lib/{name}.flac", 10, 1, 0, 0) for name in "abc"]
    assert len(fd._merge_links(records)) == 3
    linked = [fd.FileRecord("/lib/a.flac", 10, 1, 5, 42), fd.FileRecord("/lib/b.flac", 10, 1, 5, 42)]
    merged = fd._merge_links(linked)
    assert len(merged) == 1 and merged[0].aliases == ["/lib/b.flac"]


# Interface
@pytest.fixture
def demo(tmp_path, app):
    from musicscope.advanced_gui import AdvancedWindow
    from musicscope.demo import create_demo
    root = create_demo(tmp_path / "musique")
    window = AdvancedWindow(tmp_path / "cache")
    window.root = str(root)
    window.start_scan()
    settle(window, app)
    yield window, root, app
    window.close()


def test_exact_search_starts_after_yes_even_when_the_estimate_task_already_ended(demo, monkeypatch):
    window, root, app = demo

    def answer_later(*args):
        # Boîte modale réelle : la boucle d'événements termine la tâche d'estimation avant la réponse.
        settle(window, app, timeout=10)
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr(QMessageBox, "question", answer_later)
    window.start_exact()
    settle(window, app)
    assert window.exact_groups


def test_file_actions_and_buttons_are_disabled_during_a_task(demo):
    window, root, app = demo
    window.run_task("Tâche lente…", lambda cancel, progress: time.sleep(0.3), lambda result: None)
    assert all(not action.isEnabled() for action in window.task_actions)
    assert not window.jellyfin_button.isEnabled()
    assert not window.file_duplicates_page.selection_button.isEnabled()
    before = window.root
    window.choose_root()
    assert window.root == before and "en cours" in window.state.text()
    settle(window, app)
    assert all(action.isEnabled() for action in window.task_actions)
    assert window.jellyfin_button.isEnabled()


def test_escape_does_not_cancel_a_non_cancellable_task(demo):
    window, root, app = demo
    seen = {}

    def operation(cancel, progress):
        time.sleep(0.3)
        seen["cancelled"] = cancel.is_set()
        return "fini"

    window.show()
    app.processEvents()
    window.run_task("Écriture…", operation, lambda result: window.state.setText("Terminé"), cancellable=False)
    window.activateWindow()
    QTest.keyClick(window, Qt.Key.Key_Escape)
    app.processEvents()
    settle(window, app)
    assert seen == {"cancelled": False} and window.state.text() == "Terminé"


def test_exporting_twice_does_not_stack_status_prefixes(demo):
    window, root, app = demo
    window.additional_findings = [{"catégorie": "AcoustID", "dossier": "/x", "statut": "Indice d’enregistrement"}]
    window.requires_rescan = True
    for _ in range(3):
        rows = [row for row in window.findings() if row["catégorie"] == "AcoustID"]
    assert rows[0]["statut"] == "À actualiser — Indice d’enregistrement"
    assert window.additional_findings[0]["statut"] == "Indice d’enregistrement"
    window._mark_summary_stale()
    window._mark_summary_stale()
    assert window.summary.text().count("INVENTAIRE À ACTUALISER") == 1


@pytest.mark.parametrize("name", ["review.sqlite3", "tags.sqlite3"])
def test_corrupt_local_database_does_not_prevent_startup(tmp_path, app, name):
    from musicscope.advanced_gui import AdvancedWindow
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / name).write_bytes(b"ceci n'est pas une base SQLite" * 100)
    window = AdvancedWindow(cache_dir)
    try:
        assert name in window.state.text() and "illisible" in window.state.text()
    finally:
        window.close()


def test_restart_recomputes_matches_from_the_cached_inventory(demo, tmp_path):
    from musicscope.advanced_gui import AdvancedWindow
    window, root, app = demo
    expected = len(window.matches)
    groups = len(window.duplicate_groups)
    assert expected and groups
    window.save_preferences(root=window.root)
    restarted = AdvancedWindow(window.cache_dir)
    try:
        assert restarted.inventory.albums == []  # fenêtre affichée avant le chargement
        settle(restarted, app)
        assert len(restarted.matches) == expected and len(restarted.duplicate_groups) == groups
        assert "chargé depuis le cache" in restarted.state.text()
    finally:
        restarted.close()


# Session
def test_large_session_is_exported_compact(tmp_path):
    from musicscope.session import export_session, import_session
    root = tmp_path / "lib"
    albums = [Album(str(root / f"A{index}"), [Track(str(root / f"A{index}" / f"{number}.flac"), "FLAC", 1000, 1, 200.0,
                                                    {"title": [f"Titre {number}"], "album": [f"Album {index}"]})
                                              for number in range(10)]) for index in range(50)]
    target = tmp_path / "session.json"
    export_session(target, Inventory(str(root), albums, scanned_at="2026-11-16T00:00:00+00:00"), [], [], [])
    text = target.read_text("utf-8")
    assert "\n  " not in text  # plus d'indentation : environ deux fois plus petit
    assert len(import_session(target).inventory.albums) == 50
