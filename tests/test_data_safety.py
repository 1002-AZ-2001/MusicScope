"""Protection des données (v0.10.1) : régressions issues de l'audit.

Chaque test reproduit un scénario qui pouvait retirer la mauvaise copie, toutes les
copies d'un morceau, ou bloquer une restauration.
"""
import errno
import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from threading import Event

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QInputDialog

import musicscope
from musicscope import file_duplicates as fd
from musicscope import organizer as org
from musicscope.comparison import candidates, compare_albums
from musicscope.duplicates import UNSURE, duplicate_groups, score_album
from musicscope.models import Album, Track

TONE = Path(musicscope.__file__).parent / "data" / "tone-a.flac"
TAGS = {"title": ["T"], "album": ["Alb"], "albumartist": ["Art"], "artist": ["Art"], "date": ["2020"],
        "tracknumber": ["1"]}


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


def synthetic(path, mbid=None, codec="FLAC", size=1000):
    tracks = []
    for number in range(1, 11):
        tags = {"title": [f"Song {number}"], "album": ["Album"], "albumartist": ["Artist"], "artist": ["Artist"],
                "tracknumber": [str(number)], "date": ["2014"]}
        if mbid:
            tags["musicbrainz_albumid"] = [mbid]
        tracks.append(Track(f"{path}/{number:02d}.flac", codec=codec, size=size + number, mtime_ns=1,
                            duration=180.0 + number, tags=tags))
    return Album(path, tracks)


def real_album(base, name="Old", files=1):
    source = base / "lib" / name
    source.mkdir(parents=True)
    tracks = []
    for number in range(1, files + 1):
        path = source / f"0{number}.flac"
        shutil.copy(TONE, path)
        info = path.stat()
        tracks.append(Track(str(path), size=info.st_size, mtime_ns=info.st_mtime_ns, device=info.st_dev,
                            inode=info.st_ino, tags=dict(TAGS, tracknumber=[str(number)])))
    return source, Album(str(source), tracks)


def record(path):
    info = Path(path).lstat()
    return fd.FileRecord(str(path), info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino, nlink=info.st_nlink)


# Albums en double
def test_distinct_editions_joined_through_a_third_copy_are_never_preselected():
    first = synthetic("/lib/Artist/Album (2014)", mbid="11111111-1111-1111-1111-111111111111")
    copy = synthetic("/lib/Artist/Album (2014) [copy]")
    other = synthetic("/lib/Artist/Album (2016)", mbid="22222222-2222-2222-2222-222222222222", codec="MP3")
    pairs = [(left, right, compare_albums(left, right)) for left, right in candidates([first, copy, other])]
    [group] = duplicate_groups(pairs)
    assert len(group.albums) == 3
    assert group.confidence == UNSURE and group.keep is None and group.to_remove() == []
    assert any("éditions jugées distinctes" in reason for reason in group.reasons)


def test_recoverable_space_ignores_tracks_linked_elsewhere(tmp_path):
    kept_folder, kept = real_album(tmp_path, "Kept")
    linked_folder, linked = real_album(tmp_path, "Linked")
    (tmp_path / "torrents").mkdir()
    os.link(linked.tracks[0].path, tmp_path / "torrents" / "01.flac")
    scores = [score_album(kept), score_album(linked)]
    assert scores[1].linked_bytes == linked.tracks[0].size and scores[0].linked_bytes == 0
    from musicscope.duplicates import EXACT, DuplicateGroup
    group = DuplicateGroup([kept, linked], EXACT, scores, 0)
    assert group.extra_bytes == 0  # la copie à retirer est liée : rien n'est libéré
    group.keep = 1
    assert group.extra_bytes == kept.tracks[0].size


# Rangement et quarantaine d'albums : restauration
def test_album_restore_survives_removed_hardlink_and_new_inodes(tmp_path):
    source, album = real_album(tmp_path, files=2)
    (tmp_path / "downloads").mkdir()
    os.link(source / "01.flac", tmp_path / "downloads" / "01.flac")
    plan = org.plan_album(album, tmp_path / "lib")
    journal = org.apply_plan(plan, tmp_path / "journals", confirmed=True, allow_hardlinks=True)
    (tmp_path / "downloads" / "01.flac").unlink()  # qBittorrent retire son lien
    destination = Path(plan.destination)
    # Montage SMB sans « serverino » : même contenu, nouveaux inodes.
    copy = destination.with_name("copie")
    shutil.copytree(destination, copy, copy_function=shutil.copy2)
    shutil.rmtree(destination)
    os.rename(copy, destination)
    org.restore_journal(journal, confirmed=True)
    assert sorted(path.name for path in source.iterdir()) == ["01.flac", "02.flac"]
    assert json.loads(journal.read_text())["state"] == "restored"


def test_album_restore_still_refuses_changed_content(tmp_path):
    source, album = real_album(tmp_path)
    plan = org.plan_album(album, tmp_path / "lib")
    journal = org.apply_plan(plan, tmp_path / "journals", confirmed=True)
    with open(Path(plan.destination) / "01.flac", "ab") as stream:
        stream.write(b"x")
    with pytest.raises(org.OrganizeError, match="a changé"):
        org.restore_journal(journal, confirmed=True)
    assert not source.exists()


def test_interrupted_move_leaves_a_reservation_that_restore_removes(tmp_path, monkeypatch):
    source, album = real_album(tmp_path)
    plan = org.plan_album(album, tmp_path / "lib")
    destination = Path(plan.destination)

    def killed(src, dst, expected, *, is_dir=True):
        os.mkdir(dst, 0o700)  # réservation du repli SMB, puis arrêt brutal
        raise SystemExit(9)

    monkeypatch.setattr(org, "_rename_without_replace", killed)
    with pytest.raises(SystemExit):
        org.apply_plan(plan, tmp_path / "journals", confirmed=True)
    monkeypatch.undo()
    journal = next((tmp_path / "journals").glob("*.json"))
    assert json.loads(journal.read_text())["state"] == "intent" and destination.is_dir()
    org.restore_journal(journal, confirmed=True)
    assert not destination.exists() and (source / "01.flac").is_file()
    assert not org.plan_album(album, tmp_path / "lib").blockers


def test_interrupted_restore_reservation_is_removed_on_retry(tmp_path):
    source, album = real_album(tmp_path)
    plan = org.plan_album(album, tmp_path / "lib")
    journal = org.apply_plan(plan, tmp_path / "journals", confirmed=True)
    data = json.loads(journal.read_text())
    data["state"] = "restoring"
    journal.write_text(json.dumps(data))
    source.mkdir()  # réservation vide laissée à l'emplacement d'origine
    org.restore_journal(journal, confirmed=True)
    assert (source / "01.flac").is_file() and not Path(plan.destination).exists()


def test_reservation_with_content_is_never_removed(tmp_path):
    source, album = real_album(tmp_path)
    plan = org.plan_album(album, tmp_path / "lib")
    journal = org.apply_plan(plan, tmp_path / "journals", confirmed=True)
    data = json.loads(journal.read_text())
    data["state"] = "restoring"
    journal.write_text(json.dumps(data))
    source.mkdir()
    (source / "autre.txt").write_text("données")
    with pytest.raises(org.OrganizeError, match="bloquée"):
        org.restore_journal(journal, confirmed=True)
    assert (source / "autre.txt").read_text() == "données"


def test_failure_after_successful_rename_is_reported_as_applied(tmp_path, monkeypatch):
    source, album = real_album(tmp_path)
    plan = org.plan_album(album, tmp_path / "lib")
    real_save, calls = org._save_journal, []

    def flaky(path, data):
        calls.append(data["state"])
        if len(calls) == 2:
            raise OSError(errno.ENOSPC, "disque plein")
        return real_save(path, data)

    monkeypatch.setattr(org, "_save_journal", flaky)
    journal = org.apply_plan(plan, tmp_path / "journals", confirmed=True)
    monkeypatch.undo()
    assert not source.exists() and Path(plan.destination).is_dir()
    data = json.loads(journal.read_text())
    assert data["state"] == "applied" and "disque plein" in data["error"]
    org.restore_journal(journal, confirmed=True)
    assert (source / "01.flac").is_file()


# Fichiers en double : quarantaine et restauration
def test_file_restore_removes_empty_reservation_at_origin(tmp_path):
    root = tmp_path / "lib"
    (root / "A").mkdir(parents=True)
    path = root / "A" / "01.flac"
    shutil.copy(TONE, path)
    journal, moved, failed = fd.quarantine_files([record(path)], root, tmp_path / "journals")
    assert moved and not failed
    path.touch()  # restauration interrompue après la réservation (repli SMB)
    restored, failed = fd.restore_file_journal(journal)
    assert restored == [str(path)] and not failed
    assert path.stat().st_size == TONE.stat().st_size
    assert fd.quarantine_summary(root)[1] == 0


def test_file_quarantine_and_restore_report_progress_when_cancelled(tmp_path):
    root = tmp_path / "lib"
    paths = []
    for number in range(3):
        path = root / "A" / f"{number}.flac"
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(TONE, path)
        paths.append(path)
    cancel = Event()
    journal, moved, failed = fd.quarantine_files([record(path) for path in paths], root, tmp_path / "journals",
                                                 cancel, lambda *args: cancel.set())
    assert moved == [str(paths[0])] and not failed  # pas d'exception : l'interface voit le déplacement
    journal, moved, failed = fd.quarantine_files([record(path) for path in paths[1:]], root, tmp_path / "journals")
    journal_state = lambda: [entry["state"] for entry in json.loads(journal.read_text())["entries"]]
    cancel.clear()
    restored, failed = fd.restore_file_journal(journal, cancel, lambda *args: cancel.set())
    assert len(restored) == 1 and not failed
    assert journal_state() == ["restored", "moved"]  # journal fidèle au disque après l'arrêt
    restored, failed = fd.restore_file_journal(journal)
    assert len(restored) == 1 and not failed and journal_state() == ["restored", "restored"]


def test_groups_without_kept_copy_detects_a_stale_list(tmp_path):
    root = tmp_path / "lib"
    first, second = root / "A" / "01.flac", root / "B" / "01.flac"
    for path in (first, second):
        path.parent.mkdir(parents=True)
        shutil.copy(TONE, path)
    group = fd.FileGroup([record(first), record(second)], (fd.CONTENT,))
    assert fd.groups_without_kept_copy([group], {str(second)}) == []
    first.rename(tmp_path / "ailleurs.flac")  # copie gardée déplacée depuis la recherche
    assert fd.groups_without_kept_copy([group], {str(second)}) == [group]
    assert fd.groups_without_kept_copy([group], set()) == []


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


def test_file_duplicates_list_is_cleared_by_a_new_inventory_and_guarded(demo):
    from musicscope.organizer import QUARANTINE_DIR
    window, root, app = demo
    page = window.file_duplicates_page
    page.search()
    settle(window, app)
    groups = page.groups
    assert groups
    window.start_scan()
    settle(window, app)
    assert page.groups == [] and "relancez" in page.result_label.text()
    # Liste périmée remise de force : le garde-fou relit le disque avant tout déplacement.
    page.render(groups)
    kept = groups[0].best()
    Path(kept.path).rename(Path(kept.path).with_suffix(".deplace"))
    page.apply_selection({record.path for record in groups[0].files if record is not kept})
    page.quarantine(confirm=True)
    settle(window, app)
    assert "périmée" in window.state.text()
    assert not (root / QUARANTINE_DIR).exists()


def test_album_quarantine_then_stale_file_list_keeps_one_copy(demo):
    from musicscope.organizer import QUARANTINE_DIR
    window, root, app = demo
    page = window.file_duplicates_page
    page.search()
    settle(window, app)
    reference = root / "Artiste Démo" / "Horizons FLAC" / "01 - Aube.flac"
    digest = hashlib.sha256(reference.read_bytes()).hexdigest()
    group = window.duplicate_groups[0]
    keep = next(index for index, album in enumerate(group.albums) if album.path.endswith("Copie noms différents"))
    window.choose_kept_copy(group_index=0, keep=keep)
    window.duplicate_table.selectRow(0)
    app.processEvents()
    window.quarantine_duplicates(confirm=True, allow_hardlinks=True)
    settle(window, app)
    page.apply_selection(fd.select_all_but_best(page.groups))
    page.quarantine(confirm=True)
    settle(window, app)
    left = [path for path in root.rglob("*.flac") if QUARANTINE_DIR not in path.parts
            and hashlib.sha256(path.read_bytes()).hexdigest() == digest]
    assert left


def test_kept_copy_choice_distinguishes_folders_with_the_same_name(demo, monkeypatch):
    window, root, app = demo
    source = next(path for path in root.rglob("Copie noms différents") if path.is_dir())
    (root / "Téléchargements").mkdir()
    shutil.copytree(source, root / "Téléchargements" / "Copie noms différents")
    window.start_scan()
    settle(window, app)
    index, group = next((index, group) for index, group in enumerate(window.duplicate_groups)
                        if any("Téléchargements" in album.path for album in group.albums))
    wanted = next(position for position, album in enumerate(group.albums) if "Téléchargements" in album.path)
    offered = {}

    def choose(parent, title, label, items, current, editable):
        offered["items"] = items
        return items[wanted], True

    monkeypatch.setattr(QInputDialog, "getItem", staticmethod(choose))
    window.choose_kept_copy(group_index=index)
    assert len(set(offered["items"])) == len(offered["items"])
    assert group.keep == wanted
    assert all("Téléchargements" not in album.path for album in group.to_remove())


def test_cancelled_tag_write_refreshes_journal_and_inventory(demo, monkeypatch):
    from musicscope import tagwriter
    from musicscope.metadata import read_track
    window, root, app = demo
    original = tagwriter.TagJournal.apply

    def apply(self, edits, album_path, cancel=None, progress=None):
        def stop_after_first(*args):
            if progress:
                progress(*args)
            cancel.set()  # « Annuler » cliqué après le premier fichier
        return original(self, edits, album_path, cancel=cancel, progress=stop_after_first)

    monkeypatch.setattr(tagwriter.TagJournal, "apply", apply)
    row = next(index for index, album in enumerate(window.inventory.albums) if album.path.endswith("Horizons MP3"))
    window.album_table.selectRow(window.album_proxy.mapFromSource(window.album_model.index(row, 0)).row())
    app.processEvents()
    window.edit_tags(dialog_values=({"album": "Nouveau titre"}, {}), confirm=True)
    settle(window, app)
    assert window.tag_batch_model.rowCount() == 1
    album = next(album for album in window.inventory.albums if album.path.endswith("Horizons MP3"))
    on_disk = [read_track(Path(track.path)).tags.get("album") for track in album.ordered_tracks]
    assert on_disk == [track.tags.get("album") for track in album.ordered_tracks]  # inventaire actualisé
    assert ["Nouveau titre"] in on_disk and ["Horizons"] in on_disk
