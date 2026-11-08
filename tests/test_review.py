from copy import deepcopy
from dataclasses import asdict
import os
from pathlib import Path

import pytest

from musicscope.cache import Cache
from musicscope.models import Album, Inventory, Track
from musicscope.review import (
    DECISIONS, ReviewStore, album_signature, hashing_cost, quality_findings, storage_summary,
)
from musicscope.identity import file_identity


def make_album(directory: Path, content=b"music"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "01.flac"
    path.write_bytes(content)
    info = path.stat()
    device, inode = file_identity(path, info)
    return Album(str(directory), [Track(str(path), "FLAC", info.st_size, info.st_mtime_ns, 180.0,
        {"title": ["Aube"], "album": ["Horizons"], "artist": ["Artiste"],
         "albumartist": ["Artiste"], "date": ["2024"], "tracknumber": ["1"]}, device, inode)])


def test_decision_persisted_symmetric_and_note_unicode(tmp_path):
    left, right = make_album(tmp_path / "music" / "A"), make_album(tmp_path / "music" / "B")
    with ReviewStore(tmp_path / "review.sqlite") as store:
        saved = store.record(left, right, "Conserver les deux", "édition japonaise 東京")
        assert not saved.stale
    with ReviewStore(tmp_path / "review.sqlite") as store:
        found = store.get(right, left)
        assert found.decision == "Conserver les deux"
        assert found.note == "édition japonaise 東京"
        assert not found.stale
        assert store.list(Inventory(str(tmp_path / "music"), [left, right])) == [found]


def test_decision_stale_when_snapshot_or_file_changes(tmp_path):
    left, right = make_album(tmp_path / "A"), make_album(tmp_path / "B")
    with ReviewStore(tmp_path / "review.sqlite") as store:
        store.record(left, right, "À revoir")
        changed = deepcopy(right)
        changed.tracks[0].tags["album"] = ["Autre édition"]
        assert store.get(left, changed).stale
        assert not store.get(left, right).stale
        Path(right.tracks[0].path).write_bytes(b"changed source")
        assert store.get(left, right).stale


def test_decision_missing_album_stale_and_other_root_omitted(tmp_path):
    root = tmp_path / "music"
    left, right = make_album(root / "A"), make_album(root / "B")
    elsewhere = make_album(tmp_path / "other" / "A"), make_album(tmp_path / "other" / "B")
    with ReviewStore(tmp_path / "review.sqlite") as store:
        store.record(left, right, "Faux positif")
        store.record(*elsewhere, "À revoir")
        decisions = store.list(Inventory(str(root), [left]))
        assert len(decisions) == 1
        assert decisions[0].stale


def test_session_export_with_decisions_from_two_libraries_is_scoped(tmp_path):
    from musicscope.session import export_session, import_session
    first = make_album(tmp_path / "library1" / "A"), make_album(tmp_path / "library1" / "B")
    second = make_album(tmp_path / "library2" / "A"), make_album(tmp_path / "library2" / "B")
    with ReviewStore(tmp_path / "review.sqlite") as store:
        store.record(*first, "Conserver les deux")
        store.record(*second, "Faux positif")
        inventory = Inventory(str(tmp_path / "library2"), list(second))
        path = tmp_path / "session.json"
        export_session(path, inventory, decisions=store.list(inventory))
    restored = import_session(path)
    assert len(restored.decisions) == 1
    assert restored.decisions[0]["decision"] == "Faux positif"


def test_decision_unknown_and_same_album_rejected(tmp_path):
    left, right = make_album(tmp_path / "A"), make_album(tmp_path / "B")
    with ReviewStore(tmp_path / "review.sqlite") as store:
        assert store.get(left, right) is None
        with pytest.raises(ValueError):
            store.record(left, right, "Supprimer tout")
        with pytest.raises(ValueError):
            store.record(left, left, DECISIONS[0])
        with pytest.raises(ValueError):
            store.record(left, right, DECISIONS[0], "a" * 10001)


def test_signature_independent_track_order_and_sensitive_to_completeness(tmp_path):
    album = make_album(tmp_path / "A")
    album.tracks.append(deepcopy(album.tracks[0]))
    album.tracks[-1].path += ".copy"
    signature = album_signature(album)
    album.tracks.reverse()
    assert album_signature(album) == signature
    album.complete = False
    assert album_signature(album) != signature


def test_quality_prioritizes_illread_incomplete_and_conflicts_over_tags(tmp_path):
    album = make_album(tmp_path / "A")
    album.complete = False
    album.issues = ["Fichier illisible : broken.flac", "Regroupement ambigu : artistes mélangés"]
    del album.tracks[0].tags["title"]
    result = quality_findings(Inventory(str(tmp_path), [album], [album.issues[0]]))
    assert result[0].category == "Lecture"
    assert [row.priority for row in result] == sorted(row.priority for row in result)
    categories = {row.category for row in result}
    assert categories >= {"Lecture", "Inventaire incomplet", "Incohérence", "Tags manquants"}
    assert sum(row.message == album.issues[0] for row in result) == 1


def test_storage_counts_hardlinks_once_and_exact_copies(tmp_path):
    left, right = make_album(tmp_path / "A"), make_album(tmp_path / "B")
    if left.tracks[0].inode is None:
        pytest.skip("Identité physique locale indisponible")
    linked_path = tmp_path / "C" / "01.flac"
    linked_path.parent.mkdir()
    try:
        os.link(left.tracks[0].path, linked_path)
    except OSError:
        pytest.skip("Liens physiques indisponibles")
    linked = deepcopy(left)
    linked.path = str(linked_path.parent)
    linked.tracks[0].path = str(linked_path)
    inventory = Inventory(str(tmp_path), [left, right, linked])
    summary = storage_summary(inventory, [[left.tracks[0], right.tracks[0], linked.tracks[0]]])
    assert summary.apparent_bytes == 15
    assert summary.known_unique_bytes == 10
    assert summary.verified_duplicate_bytes == 5


def test_storage_unknown_remote_or_changed_source_cannot_claim_recoverable(tmp_path):
    album = make_album(tmp_path / "A")
    album.tracks[0].device = album.tracks[0].inode = None
    summary = storage_summary(Inventory(str(tmp_path), [album]))
    assert summary.apparent_bytes == 5
    assert summary.known_unique_bytes is None
    assert summary.verified_duplicate_bytes is None
    assert "indéterminés" in " ".join(summary.reasons)


def test_storage_external_hardlink_keeps_recovery_indeterminate(tmp_path):
    left, right = make_album(tmp_path / "A"), make_album(tmp_path / "B")
    if left.tracks[0].inode is None:
        pytest.skip("Identité physique locale indisponible")
    try:
        os.link(left.tracks[0].path, tmp_path / "outside-link.flac")
    except OSError:
        pytest.skip("Liens physiques indisponibles")
    summary = storage_summary(Inventory(str(tmp_path), [left, right]), [[left.tracks[0], right.tracks[0]]])
    assert summary.known_unique_bytes == 10
    assert summary.verified_duplicate_bytes is None
    assert "hors de l'inventaire" in " ".join(summary.reasons)


def test_hashing_cost_size_prefilter_and_persistent_progress(tmp_path):
    left, right = make_album(tmp_path / "A"), make_album(tmp_path / "B")
    third = make_album(tmp_path / "C", b"larger-content")
    inventory = Inventory(str(tmp_path), [left, right, third])
    with Cache(tmp_path / "cache.sqlite") as cache:
        cost = hashing_cost(inventory, cache)
        assert asdict(cost) == {"candidate_files": 2, "candidate_bytes": 10, "uncached_bytes": 10, "stale_files": 0}
        cache.hash_track(left.tracks[0])
        assert hashing_cost(inventory, cache).uncached_bytes == 5
        cache.hash_track(right.tracks[0])
        assert hashing_cost(inventory, cache).uncached_bytes == 0
        Path(right.tracks[0].path).write_bytes(b"new music")
        stale = hashing_cost(inventory, cache)
        assert stale.stale_files == 1 and stale.uncached_bytes == 5
