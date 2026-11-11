"""Écriture de tags : cinq formats, journal, vérification par relecture et annulation."""
from __future__ import annotations

import os
import shutil
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import pytest
from mutagen.id3 import ID3

from musicscope import tagwriter
from musicscope.cache import Cache
from musicscope.metadata import read_track
from musicscope.scanner import scan
from musicscope.tagwriter import (
    TagJournal, _Tags, describe, plan_album_edits, proposals_to_values, split_values,
)

FIXTURES = Path(__file__).parent / "data"
SOURCES = {
    "flac": files("musicscope").joinpath("data/tone-a.flac"),
    "mp3": files("musicscope").joinpath("data/tone-a.mp3"),
    "m4a": FIXTURES / "tone.m4a",
    "ogg": FIXTURES / "tone.ogg",
    "opus": FIXTURES / "tone.opus",
}


def make_album(tmp_path, formats=tuple(SOURCES)):
    folder = tmp_path / "library" / "Artiste" / "Album"
    folder.mkdir(parents=True)
    for number, fmt in enumerate(formats, 1):
        target = folder / f"{number:02d}.{fmt}"
        shutil.copyfile(str(SOURCES[fmt]), target)
        tags = _Tags(target)
        for key, value in {"album": ["Ancien album"], "albumartist": ["Ancien artiste"], "title": [f"Piste {number}"],
                           "tracknumber": [str(number)], "artist": ["Ancien artiste"]}.items():
            tags.set(key, value)
        tags.save()
    with Cache(tmp_path / "cache" / "inventory.sqlite3") as cache:
        inventory = scan(tmp_path / "library", cache)
    assert not inventory.errors
    return inventory.albums[0]


def easy(path, key):
    return _Tags(Path(path)).get(key)


@pytest.mark.parametrize("fmt", list(SOURCES))
def test_round_trip_and_undo_for_each_format(tmp_path, fmt):
    album = make_album(tmp_path, (fmt,))
    track = album.tracks[0]
    before = read_track(Path(track.path)).tags
    other_before = {key: value for key, value in _Tags(Path(track.path)).tags.items()
                    if key not in tagwriter.EDITABLE_FIELDS} if fmt != "mp3" else None
    edits = plan_album_edits(album, {"album": "Nouvel album", "genre": "Jazz", "date": "2024"},
                             {track.path: {"title": "Nouveau titre", "artist": "A; B"}})
    assert len(edits) == 1 and set(edits[0].changes) == {"album", "genre", "date", "title", "artist"}
    with TagJournal(tmp_path / "cache" / "tags.sqlite3") as journal:
        result = journal.apply(edits, album.path)
        assert result.written == [track.path] and not result.failed
        after = read_track(Path(track.path)).tags
        assert after["album"] == ["Nouvel album"] and after["title"] == ["Nouveau titre"]
        assert after["genre"] == ["Jazz"] and after["date"] == ["2024"]
        assert after["artist"] == ["A", "B"]
        assert after["albumartist"] == before["albumartist"]
        if other_before is not None:
            assert {key: value for key, value in _Tags(Path(track.path)).tags.items()
                    if key not in tagwriter.EDITABLE_FIELDS} == other_before
        batch = journal.batches()[0]
        assert (batch.files, batch.written, batch.undone_at) == (1, 1, None)
        undo = journal.undo(batch.id)
        assert undo.written == [track.path] and not undo.failed
        assert read_track(Path(track.path)).tags == before
        assert journal.batches()[0].undone_at
        with pytest.raises(ValueError):
            journal.undo(batch.id)


def test_id3v23_version_is_kept_and_multiple_values_joined(tmp_path):
    album = make_album(tmp_path, ("mp3",))
    path = Path(album.tracks[0].path)
    tags = ID3(path)
    tags.save(path, v2_version=3)
    with Cache(tmp_path / "cache" / "inventory.sqlite3") as cache:
        album = scan(tmp_path / "library", cache).albums[0]
    edits = plan_album_edits(album, {}, {str(path): {"artist": "A; B"}})
    with TagJournal(tmp_path / "tags.sqlite3") as journal:
        assert journal.apply(edits, album.path).written == [str(path)]
    assert ID3(path).version[:2] == (2, 3)
    assert easy(path, "artist") == ["A; B"]


def test_changed_file_is_refused_and_untouched(tmp_path):
    album = make_album(tmp_path, ("flac",))
    track = album.tracks[0]
    edits = plan_album_edits(album, {"album": "Nouvel album"}, {})
    os.utime(track.path, ns=(track.mtime_ns + 5_000_000_000, track.mtime_ns + 5_000_000_000))
    with TagJournal(tmp_path / "tags.sqlite3") as journal:
        result = journal.apply(edits, album.path)
        assert not result.written and "modifié depuis l’analyse" in result.failed[0][1]
        assert journal.entries(result.batch_id) == []
    assert easy(track.path, "album") == ["Ancien album"]


def test_failed_verification_restores_original_values(tmp_path, monkeypatch):
    album = make_album(tmp_path, ("flac",))
    track = album.tracks[0]
    real_write = tagwriter._write
    calls = []

    def flaky(path, values):
        calls.append(values)
        real_write(path, values)
        if len(calls) == 1:
            raise ValueError("relecture différente")

    monkeypatch.setattr(tagwriter, "_write", flaky)
    edits = plan_album_edits(album, {"album": "Nouvel album"}, {})
    with TagJournal(tmp_path / "tags.sqlite3") as journal:
        result = journal.apply(edits, album.path)
        assert not result.written and result.failed
        assert journal.entries(result.batch_id)[0][3] == "failed_restored"
    assert easy(track.path, "album") == ["Ancien album"]


def test_undo_refused_when_tags_changed_after_writing(tmp_path):
    album = make_album(tmp_path, ("flac",))
    track = album.tracks[0]
    with TagJournal(tmp_path / "tags.sqlite3") as journal:
        result = journal.apply(plan_album_edits(album, {"album": "Nouvel album"}, {}), album.path)
        tags = _Tags(Path(track.path))
        tags.set("album", ["Changé ailleurs"])
        tags.save()
        undo = journal.undo(result.batch_id)
        assert not undo.written and "modifiés depuis" in undo.failed[0][1]
        assert journal.batches()[0].undone_at is None
    assert easy(track.path, "album") == ["Changé ailleurs"]


def test_empty_value_deletes_field_and_undo_restores_it(tmp_path):
    album = make_album(tmp_path, ("ogg",))
    track = album.tracks[0]
    with TagJournal(tmp_path / "tags.sqlite3") as journal:
        result = journal.apply(plan_album_edits(album, {"albumartist": ""}, {}), album.path)
        assert result.written
        assert easy(track.path, "albumartist") == []
        journal.undo(result.batch_id)
    assert easy(track.path, "albumartist") == ["Ancien artiste"]


def test_plan_keeps_only_real_changes(tmp_path):
    album = make_album(tmp_path, ("flac", "mp3"))
    assert plan_album_edits(album, {"album": "Ancien album"}, {}) == []
    edits = plan_album_edits(album, {"album": "  Ancien album  "}, {album.ordered_tracks[1].path: {"title": "X"}})
    assert [edit.path for edit in edits] == [album.ordered_tracks[1].path]
    assert describe(edits) == [("02.mp3", "Titre", "Piste 2", "X")]
    with pytest.raises(ValueError):
        plan_album_edits(album, {"musicbrainz_albumid": "x"}, {})
    assert split_values(" A ;B;; ") == ["A", "B"]


def test_cancel_stops_between_files(tmp_path):
    from threading import Event
    from musicscope.cache import ScanCancelled
    album = make_album(tmp_path, ("flac", "ogg"))
    cancel = Event()
    edits = plan_album_edits(album, {"album": "Nouvel album"}, {})

    def stop(done, total, path):
        cancel.set()

    with TagJournal(tmp_path / "tags.sqlite3") as journal, pytest.raises(ScanCancelled):
        journal.apply(edits, album.path, cancel=cancel, progress=stop)
    assert [easy(track.path, "album") for track in album.ordered_tracks] == [["Nouvel album"], ["Ancien album"]]


def test_musicbrainz_proposals_limited_to_editable_fields(tmp_path):
    album = make_album(tmp_path, ("flac", "mp3"))
    first, second = album.ordered_tracks
    proposals = [SimpleNamespace(path=first.path, field="album", proposed="Vrai album"),
                 SimpleNamespace(path=second.path, field="album", proposed="Vrai album"),
                 SimpleNamespace(path=first.path, field="title", proposed="Vrai titre"),
                 SimpleNamespace(path=first.path, field="musicbrainz_albumid", proposed="mbid")]
    album_values, track_values = proposals_to_values(proposals, album)
    assert album_values == {"album": "Vrai album"}
    assert track_values == {first.path: {"title": "Vrai titre"}}
