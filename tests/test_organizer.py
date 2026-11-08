from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from musicscope.models import Album, Track
from musicscope.organizer import (
    OrganizeError, apply_plan, plan_album, restore_journal, sanitize_component,
)


def make_album(tmp_path, title="Voyage", artist="Écho", date="2024"):
    source = tmp_path / "library" / "Old"
    source.mkdir(parents=True)
    audio = source / "01.flac"
    audio.write_bytes(b"synthetic audio bytes")
    (source / "cover.jpg").write_bytes(b"synthetic cover bytes")
    (source / "CD2").mkdir()
    (source / "CD2" / "notes.txt").write_text("préserver", encoding="utf-8")
    info = audio.stat()
    track = Track(str(audio), size=info.st_size, mtime_ns=info.st_mtime_ns,
                  tags={"title": ["Départ"], "album": [title], "albumartist": [artist],
                        "artist": [artist], "date": [date], "tracknumber": ["1"]})
    return Album(str(source), [track])


def contents(folder):
    return {str(path.relative_to(folder)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in folder.rglob("*") if path.is_file()}


def test_preview_readonly_move_restore_full_tree(tmp_path):
    album = make_album(tmp_path)
    source = Path(album.path)
    before = contents(source)
    destination_root = tmp_path / "library"
    plan = plan_album(album, destination_root)
    assert not plan.blockers
    assert Path(plan.destination) == destination_root / "Écho" / "Voyage (2024)"
    assert not Path(plan.destination).exists()
    assert contents(source) == before
    with pytest.raises(OrganizeError, match="confirmation"):
        apply_plan(plan, tmp_path / "journals")
    assert not (tmp_path / "journals").exists()
    journal = apply_plan(plan, tmp_path / "journals", confirmed=True)
    assert json.loads(journal.read_text())["state"] == "applied"
    assert not source.exists()
    assert contents(Path(plan.destination)) == before
    with pytest.raises(OrganizeError, match="confirmation"):
        restore_journal(journal)
    restore_journal(journal, confirmed=True)
    assert json.loads(journal.read_text())["state"] == "restored"
    assert contents(source) == before
    assert not Path(plan.destination).exists()
    # Une restauration reprise après succès est sans effet destructeur.
    restore_journal(journal, confirmed=True)
    assert contents(source) == before


def test_collision_plan_and_concurrent_destination_never_overwritten(tmp_path):
    album = make_album(tmp_path)
    plan = plan_album(album, tmp_path / "target")
    destination = Path(plan.destination)
    destination.mkdir(parents=True)
    sentinel = destination / "important.txt"
    sentinel.write_text("untouched")
    assert any("existe" in issue for issue in plan_album(album, tmp_path / "target").blockers)
    with pytest.raises(OrganizeError, match="Destination"):
        apply_plan(plan, tmp_path / "journals", confirmed=True)
    assert Path(album.path).exists()
    assert sentinel.read_text() == "untouched"


def test_race_at_atomic_rename_no_replace(tmp_path, monkeypatch):
    import musicscope.organizer as organizer
    album = make_album(tmp_path)
    plan = plan_album(album, tmp_path / "target")
    original = organizer._rename_without_replace

    def race(source, destination, expected):
        destination.mkdir()
        (destination / "sentinel.txt").write_text("preserve")
        original(source, destination, expected)

    monkeypatch.setattr(organizer, "_rename_without_replace", race)
    with pytest.raises(OrganizeError, match="interrompu") as caught:
        apply_plan(plan, tmp_path / "journals", confirmed=True)
    assert Path(album.path).exists()
    assert (Path(plan.destination) / "sentinel.txt").read_text() == "preserve"
    record = json.loads(caught.value.journal_path.read_text())
    assert record["state"] == "failed"
    assert record["source"] == album.path


@pytest.mark.parametrize("mutation", ["content", "addition", "removal"])
def test_stale_tree_refused_before_journal_or_source_mutation(tmp_path, mutation):
    album = make_album(tmp_path)
    plan = plan_album(album, tmp_path / "target")
    source = Path(album.path)
    if mutation == "content":
        path = source / "cover.jpg"
        before = path.stat()
        path.write_bytes(b"X" * before.st_size)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    elif mutation == "addition":
        (source / "new file.txt").write_text("new")
    else:
        (source / "cover.jpg").unlink()
    with pytest.raises(OrganizeError, match="changé"):
        apply_plan(plan, tmp_path / "journals", confirmed=True)
    assert source.exists()
    assert not Path(plan.destination).exists()
    assert not (tmp_path / "journals").exists()


def test_restore_refuses_changed_destination(tmp_path):
    album = make_album(tmp_path)
    plan = plan_album(album, tmp_path / "target")
    journal = apply_plan(plan, tmp_path / "journals", confirmed=True)
    (Path(plan.destination) / "cover.jpg").write_bytes(b"changed")
    with pytest.raises(OrganizeError, match="changé"):
        restore_journal(journal, confirmed=True)
    assert Path(plan.destination).exists()
    assert not Path(album.path).exists()


def test_restore_refuses_occupied_original_path(tmp_path):
    album = make_album(tmp_path)
    plan = plan_album(album, tmp_path / "target")
    journal = apply_plan(plan, tmp_path / "journals", confirmed=True)
    source = Path(album.path)
    source.mkdir()
    (source / "important").write_text("keep")
    with pytest.raises(OrganizeError, match="bloquée"):
        restore_journal(journal, confirmed=True)
    assert (source / "important").read_text() == "keep"
    assert Path(plan.destination).exists()


def test_hardlinks_require_distinct_ack_and_remain_shared(tmp_path):
    album = make_album(tmp_path)
    external = tmp_path / "other-import.flac"
    try:
        os.link(album.tracks[0].path, external)
    except OSError:
        pytest.skip("Liens physiques indisponibles")
    plan = plan_album(album, tmp_path / "target")
    assert plan.hardlinked_files == 1
    assert plan.warnings
    with pytest.raises(OrganizeError, match="liens physiques"):
        apply_plan(plan, tmp_path / "journals", confirmed=True)
    journal = apply_plan(plan, tmp_path / "journals", confirmed=True, allow_hardlinks=True)
    assert os.path.samefile(external, Path(plan.destination) / "01.flac")
    restore_journal(journal, confirmed=True)
    assert os.path.samefile(external, album.tracks[0].path)


@pytest.mark.parametrize("location", ["inside", "ancestor", "target_parent"])
def test_symlink_sources_or_destination_parents_blocked(tmp_path, location):
    album = make_album(tmp_path)
    root = tmp_path / "target"
    try:
        if location == "inside":
            (Path(album.path) / "alias").symlink_to(tmp_path, target_is_directory=True)
        elif location == "ancestor":
            alias = tmp_path / "source_alias"
            alias.symlink_to(Path(album.path).parent, target_is_directory=True)
            album.path = str(alias / "Old")
        else:
            root.symlink_to(Path(album.path).parent, target_is_directory=True)
    except OSError:
        pytest.skip("Liens symboliques indisponibles")
    plan = plan_album(album, root)
    assert any("Lien" in blocker for blocker in plan.blockers)


def test_destination_within_source_or_journal_within_source_blocked(tmp_path):
    album = make_album(tmp_path)
    source = Path(album.path)
    assert plan_album(album, source / "new").blockers
    plan = plan_album(album, tmp_path / "target")
    with pytest.raises(OrganizeError, match="journal"):
        apply_plan(plan, source / "journal", confirmed=True)
    assert source.exists()


def test_plan_requires_consistent_album_metadata_and_current_inventory(tmp_path):
    album = make_album(tmp_path)
    del album.tracks[0].tags["albumartist"]
    assert any("Artiste" in issue for issue in plan_album(album, tmp_path / "target").blockers)
    album.tracks[0].tags["albumartist"] = ["Artiste"]
    album.complete = False
    assert any("complet" in issue for issue in plan_album(album, tmp_path / "target").blockers)
    album.complete = True
    Path(album.tracks[0].path).write_bytes(b"replaced")
    assert any("Inventaire ancien" in issue for issue in plan_album(album, tmp_path / "target").blockers)


def test_cross_device_preview_is_blocked(tmp_path, monkeypatch):
    import musicscope.organizer as organizer
    album = make_album(tmp_path)
    original = organizer._check_ancestors
    target = tmp_path / "target"

    class OtherVolume:
        def stat(self):
            return type("Info", (), {"st_dev": -12345})()

    def probe(path, **kwargs):
        nearest = original(path, **kwargs)
        return OtherVolume() if kwargs.get("allow_missing") else nearest

    monkeypatch.setattr(organizer, "_check_ancestors", probe)
    assert any("volumes" in issue for issue in plan_album(album, target).blockers)


@pytest.mark.parametrize("raw,expected", [("CON", "_CON"), ("LPT1.txt", "_LPT1.txt"),
                                         ("../A:B?", ".._A_B_"), ("  Album. ", "Album"),
                                         ("東京", "東京"), ("..", "_")])
def test_windows_safe_components(raw, expected):
    assert sanitize_component(raw) == expected


def test_journal_intent_is_durable_before_rename(tmp_path, monkeypatch):
    import musicscope.organizer as organizer
    album = make_album(tmp_path)
    plan = plan_album(album, tmp_path / "target")
    original = organizer._rename_without_replace

    def assert_journal_first(source, destination, expected):
        journals = list((tmp_path / "journals").glob("*.json"))
        assert len(journals) == 1
        assert json.loads(journals[0].read_text())["state"] == "intent"
        original(source, destination, expected)

    monkeypatch.setattr(organizer, "_rename_without_replace", assert_journal_first)
    apply_plan(plan, tmp_path / "journals", confirmed=True)


def test_recovery_after_move_but_before_completed_journal_write(tmp_path):
    album = make_album(tmp_path)
    plan = plan_album(album, tmp_path / "target")
    journal = apply_plan(plan, tmp_path / "journals", confirmed=True)
    record = json.loads(journal.read_text())
    record["state"] = "intent"
    journal.write_text(json.dumps(record), encoding="utf-8")
    restore_journal(journal, confirmed=True)
    assert Path(album.path).exists()


def test_invalid_or_modified_journal_refused(tmp_path):
    album = make_album(tmp_path)
    plan = plan_album(album, tmp_path / "target")
    journal = apply_plan(plan, tmp_path / "journals", confirmed=True)
    record = json.loads(journal.read_text())
    record["files"][0]["inode"] += 1
    journal.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(OrganizeError, match="Empreinte"):
        restore_journal(journal, confirmed=True)
    assert Path(plan.destination).exists()


def test_entry_limit_counts_pending_empty_directories(tmp_path, monkeypatch):
    album = make_album(tmp_path)
    source = Path(album.path)
    for number in range(10):
        (source / f"empty-{number}").mkdir()
    monkeypatch.setattr("musicscope.organizer.MAX_ENTRIES", 5)
    plan = plan_album(album, tmp_path / "target")
    assert any("volumineux" in blocker for blocker in plan.blockers)
    assert source.exists()
    assert not Path(plan.destination).exists()


def test_no_source_mutation_when_initial_journal_cannot_be_written(tmp_path, monkeypatch):
    album = make_album(tmp_path)
    plan = plan_album(album, tmp_path / "target")
    before = contents(Path(album.path))

    def failed_write(*args):
        raise OSError("journal disk unavailable")

    monkeypatch.setattr("musicscope.organizer._save_journal", failed_write)
    with pytest.raises(OSError, match="journal disk"):
        apply_plan(plan, tmp_path / "journals", confirmed=True)
    assert contents(Path(album.path)) == before
    assert not Path(plan.destination).exists()


def test_parent_album_does_not_move_nested_unrelated_album(tmp_path):
    album = make_album(tmp_path)
    nested = Path(album.path) / "Other Album"
    nested.mkdir()
    (nested / "01.flac").write_bytes(b"unrelated nested album")
    plan = plan_album(album, tmp_path / "target")
    assert any("hors de cet album" in blocker for blocker in plan.blockers)
    with pytest.raises(OrganizeError, match="Plan bloqué"):
        apply_plan(plan, tmp_path / "journals", confirmed=True)
    assert (nested / "01.flac").read_bytes() == b"unrelated nested album"
    assert not Path(plan.destination).exists()


def test_known_scanned_identity_replacement_blocks_stale_metadata(tmp_path):
    album = make_album(tmp_path)
    path = Path(album.tracks[0].path)
    original = path.stat()
    album.tracks[0].device, album.tracks[0].inode = original.st_dev, original.st_ino
    replacement = path.with_suffix(".replacement")
    replacement.write_bytes(path.read_bytes())
    os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
    os.replace(replacement, path)
    if path.stat().st_ino == original.st_ino:
        pytest.skip("Identité de remplacement non disponible sur ce système")
    plan = plan_album(album, tmp_path / "target")
    assert any("Inventaire ancien" in blocker for blocker in plan.blockers)
