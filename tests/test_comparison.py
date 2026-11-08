from copy import deepcopy
import os
from pathlib import Path
import threading

import pytest

from musicscope.cache import Cache, ScanCancelled
from musicscope.comparison import (
    DISTINCT, IDENTICAL, MATCHING, PHYSICAL, PROBABLE, REVIEW,
    candidates, compare_albums, exact_file_groups,
)
from musicscope.models import Album, Inventory, Track


RELEASE_A = "00000000-0000-0000-0000-000000000001"
RELEASE_B = "00000000-0000-0000-0000-000000000002"
GROUP = "00000000-0000-0000-0000-000000000003"


def album(path="album", *, titles=("Départ", "Arrivée"), codec="FLAC", release=None, discs=None):
    tracks = []
    for i, title in enumerate(titles, 1):
        disc, number = discs[i-1] if discs else (1, i)
        tags = {"album": ["Voyage"], "albumartist": ["Écho"], "artist": ["Écho"],
                "title": [title], "tracknumber": [str(number)], "discnumber": [str(disc)]}
        if release:
            tags["musicbrainz_albumid"] = [release]
        tracks.append(Track(f"{path}/{i}.flac", codec=codec, size=123, duration=180.0, tags=tags))
    return Album(path, tracks)


def physical_track(path: Path, tags=None):
    st = path.stat()
    return Track(str(path), size=st.st_size, mtime_ns=st.st_mtime_ns,
                 device=st.st_dev, inode=st.st_ino, tags=tags or {})


def test_cross_codec_is_only_corresponding_tracks():
    a, b = album(), album("mp3", codec="MP3")
    b.tracks[0].duration += 0.4
    result = compare_albums(a, b)
    assert result.status == MATCHING
    assert any("Codecs" in d for d in result.differences)
    assert any("Identité indisponible" in r for r in result.reasons)


def test_matching_release_and_order_is_probable():
    a, b = album(release=RELEASE_A), album("copy", release=RELEASE_A)
    assert compare_albums(a, b).status == PROBABLE


def test_bonus_reissue_is_distinct_with_coherent_release_ids():
    a = album(release=RELEASE_A)
    b = album("deluxe", titles=("Départ", "Arrivée", "Bonus"), release=RELEASE_B)
    for t in b.tracks:
        t.tags["album"] = ["Voyage (Deluxe)"]
    for t in a.tracks + b.tracks:
        t.tags["musicbrainz_releasegroupid"] = [GROUP]
    result = compare_albums(a, b)
    assert result.status == DISTINCT
    assert any("bonus possible" in d and "Bonus" in d for d in result.differences)
    assert candidates([a, b]) == [(a, b)]


def test_conflicting_same_release_tracklist_is_review():
    a = album(release=RELEASE_A)
    b = album("bonus", titles=("Départ", "Arrivée", "Bonus"), release=RELEASE_A)
    assert compare_albums(a, b).status == REVIEW


def test_multidisc_order_and_duration_differences():
    a, b = album(discs=((1, 1), (2, 1))), album("copy", discs=((1, 1), (2, 1)))
    assert compare_albums(a, b).status == MATCHING
    b.tracks[1].duration += 20
    result = compare_albums(a, b)
    assert result.status == REVIEW
    assert any("Durée" in d for d in result.differences)
    b.tracks[1].duration = 180
    b.tracks[0].tags["title"], b.tracks[1].tags["title"] = b.tracks[1].tags["title"], b.tracks[0].tags["title"]
    result = compare_albums(a, b)
    assert result.status == REVIEW
    assert any("Ordre/titre" in d for d in result.differences)


@pytest.mark.parametrize("break_kind", ["flag", "gap", "tracktotal", "disctotal", "duplicate"])
def test_incomplete_album_never_definitive(break_kind):
    a, b = album(release=RELEASE_A), album("copy", release=RELEASE_A)
    if break_kind == "flag":
        b.complete = False
    elif break_kind == "gap":
        b.tracks[1].tags["tracknumber"] = ["3"]
    elif break_kind == "tracktotal":
        b.tracks[1].tags["tracknumber"] = ["2/3"]
    elif break_kind == "disctotal":
        b.tracks[0].tags["disctotal"] = ["2"]
    else:
        b.tracks[1].tags["tracknumber"] = ["1"]
    assert compare_albums(a, b).status == REVIEW


def test_contradictory_tags_and_recording_ids():
    a, b = album(release=RELEASE_A), album("copy", release=RELEASE_A)
    b.tracks[1].tags["musicbrainz_albumid"] = [RELEASE_B]
    assert compare_albums(a, b).status == REVIEW
    b = album("copy", release=RELEASE_A)
    for track in b.tracks:
        track.tags["albumartist"] = ["Autre artiste"]
    assert compare_albums(a, b).status == REVIEW
    b = album("copy", release=RELEASE_A)
    a.tracks[0].tags["musicbrainz_trackid"] = [RELEASE_A]
    b.tracks[0].tags["musicbrainz_trackid"] = [RELEASE_B]
    assert compare_albums(a, b).status == REVIEW


def test_same_release_tag_on_one_track_is_not_probable():
    a, b = album(release=RELEASE_A), album("copy", release=RELEASE_A)
    del b.tracks[1].tags["musicbrainz_albumid"]
    assert compare_albums(a, b).status == MATCHING


def test_exact_renamed_files_and_size_prefilter(tmp_path):
    one = tmp_path / "first.flac"
    two = tmp_path / "renamed.mp3"
    other = tmp_path / "other.flac"
    one.write_bytes(b"audio-content")
    two.write_bytes(one.read_bytes())
    other.write_bytes(b"different-length")
    a, b, c = Album("left", [physical_track(one)]), Album("right", [physical_track(two)]), Album("other", [physical_track(other)])
    cache = Cache(tmp_path / "cache.db")
    try:
        result = compare_albums(a, b, cache=cache, exact=True)
        assert result.status == IDENTICAL
        assert result.track_pairs == [(a.tracks[0], b.tracks[0])]
        progress = []
        groups = exact_file_groups(Inventory(str(tmp_path), [a, b, c]), cache,
                                   progress=lambda *args: progress.append(args))
        assert groups == [[a.tracks[0], b.tracks[0]]]
        assert len(progress) == 2  # le troisième fichier n'est même pas haché
    finally:
        cache.close()


def test_exact_album_matches_bijection_even_reordered(tmp_path):
    files = []
    for name, contents in (("a.flac", b"first"), ("b.flac", b"second"),
                           ("z.flac", b"first"), ("y.flac", b"second")):
        path = tmp_path / name
        path.write_bytes(contents)
        files.append(physical_track(path))
    a, b = Album("left", files[:2]), Album("right", files[2:])
    cache = Cache(tmp_path / "cache.db")
    try:
        assert compare_albums(a, b, cache, exact=True).status == IDENTICAL
        b.tracks.append(deepcopy(files[2]))
        assert compare_albums(a, b, cache, exact=True).status != IDENTICAL
    finally:
        cache.close()


def test_hardlink_identity_same_volume(tmp_path):
    one, two = tmp_path / "source.flac", tmp_path / "link.flac"
    one.write_bytes(b"hardlinked-audio")
    try:
        os.link(one, two)
    except OSError as exc:
        pytest.skip(f"Liens physiques indisponibles : {exc}")
    a, b = Album("left", [physical_track(one)]), Album("right", [physical_track(two)])
    assert compare_albums(a, b).status == PHYSICAL
    b.tracks[0].device += 1
    assert compare_albums(a, b).status != PHYSICAL
    b.tracks[0].device = None
    assert "Identité indisponible" in " ".join(compare_albums(a, b).reasons)


def test_no_definitive_physical_status_for_partial_inventory(tmp_path):
    path = tmp_path / "audio.flac"
    path.write_bytes(b"same-audio")
    a, b = Album("one", [physical_track(path)]), Album("two", [physical_track(path)], complete=False)
    assert compare_albums(a, b).status == REVIEW


def test_index_does_not_pair_empty_metadata_or_different_artists():
    a, b, c = album(), album("copy"), album("unrelated")
    for track in c.tracks:
        track.tags["albumartist"] = ["Autre"]
    assert candidates([Album("empty"), Album("empty2"), a, b, c]) == [(a, b)]
    a.tracks[0].tags["musicbrainz_albumid"] = [RELEASE_A]
    c.tracks[0].tags["musicbrainz_albumid"] = [RELEASE_A]
    assert (a, c) in candidates([a, b, c])


def test_exact_search_cancellation(tmp_path):
    cancel = threading.Event()
    cancel.set()
    cache = Cache(tmp_path / "cache.db")
    try:
        with pytest.raises(ScanCancelled):
            exact_file_groups(Inventory("empty"), cache, cancel)
        with pytest.raises(ScanCancelled):
            compare_albums(album(), album("copy"), cancel=cancel)
    finally:
        cache.close()


@pytest.mark.parametrize("key", ["tracknumber", "discnumber"])
def test_huge_number_tags_do_not_allocate_huge_ranges(key):
    a, b = album(release=RELEASE_A), album("copy", release=RELEASE_A)
    b.tracks[1].tags[key] = ["999999999999999999999999999999999999"]
    assert compare_albums(a, b).status == REVIEW


def test_joint_album_artists_are_coherent_when_same_on_every_track():
    a, b = album(release=RELEASE_A), album("copy", release=RELEASE_A)
    for track in a.tracks:
        track.tags["albumartist"] = ["Écho", "Invité"]
    for track in b.tracks:
        track.tags["albumartist"] = ["INVITÉ", "ECHO"]
    assert len(a.tag_signatures("albumartist")) == 1
    assert candidates([a, b]) == [(a, b)]
    assert compare_albums(a, b).status == PROBABLE
    b.tracks[1].tags["albumartist"] = ["ECHO"]
    assert compare_albums(a, b).status == REVIEW


def test_missing_album_artist_on_one_track_is_not_probable():
    a, b = album(release=RELEASE_A), album("copy", release=RELEASE_A)
    del b.tracks[1].tags["albumartist"]
    assert compare_albums(a, b).status == REVIEW


def test_compilation_can_have_different_original_recording_dates():
    a, b = album(release=RELEASE_A), album("copy", release=RELEASE_A)
    for item in (a, b):
        item.tracks[0].tags["originaldate"] = ["1990"]
        item.tracks[1].tags["originaldate"] = ["1995"]
    assert compare_albums(a, b).status == PROBABLE


def test_ambiguous_folder_grouping_does_not_determine_edition():
    a, b = album(release=RELEASE_A), album("copy", release=RELEASE_A)
    b.issues.append("Regroupement ambigu : CD1 contredit le numéro de disque de la piste")
    assert compare_albums(a, b).status == REVIEW


@pytest.mark.parametrize("change", ["deleted", "modified", "replaced"])
def test_physical_identity_is_revalidated_for_restored_inventory(tmp_path, change):
    one, two = tmp_path / "source.flac", tmp_path / "link.flac"
    one.write_bytes(b"original-audio")
    try:
        os.link(one, two)
    except OSError as exc:
        pytest.skip(f"Liens physiques indisponibles : {exc}")
    a, b = Album("left", [physical_track(one)]), Album("right", [physical_track(two)])
    if change == "deleted":
        two.unlink()
    elif change == "modified":
        one.write_bytes(b"new content and new length")
    else:
        two.unlink()
        two.write_bytes(b"different-audio")
    result = compare_albums(a, b)
    assert result.status != PHYSICAL
    assert any("Identité indisponible" in reason for reason in result.reasons)
