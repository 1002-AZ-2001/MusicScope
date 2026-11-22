"""MusicBrainz en lot (v0.21.0) : identifiants seulement, rien pour les albums incertains."""
import io
import json
import os
import shutil
import threading
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

import musicscope
from musicscope.cache import ScanCancelled
from musicscope.mb_batch import (
    PROBABLE,
    estimate,
    identifier_edits,
    identifiers,
    verify_albums,
)
from musicscope.musicbrainz import (
    MusicBrainzClient,
    ReleaseCandidate,
    TagProposal,
    Verification,
)
from musicscope.tagwriter import TagJournal, _Tags

DATA = Path(musicscope.__file__).parent / "data"
RELEASE = "11111111-1111-4111-8111-111111111111"
GROUP = "55555555-5555-4555-8555-555555555555"
ARTIST = "44444444-4444-4444-8444-444444444444"


def recording(number):
    return f"33333333-3333-4333-8333-33333333333{number}"


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def library(tmp_path, names=("Sûr", "Incertain")):
    from musicscope.cache import Cache
    from musicscope.scanner import scan
    for name in names:
        folder = tmp_path / "lib" / "Artiste" / name
        folder.mkdir(parents=True)
        for track in (1, 2):
            path = folder / f"0{track}.flac"
            shutil.copy(DATA / "tone-a.flac", path)
            tags = _Tags(path)
            for key, value in {"album": [name], "albumartist": ["Artiste"], "artist": ["Artiste"],
                               "title": [f"Titre {track}"], "tracknumber": [str(track)]}.items():
                tags.set(key, value)
            tags.save()
    with Cache(tmp_path / "cache.sqlite3") as cache:
        return sorted(scan(tmp_path / "lib", cache, workers=1).albums, key=lambda album: album.path)


def probable(album):
    url = f"https://musicbrainz.org/release/{RELEASE}"
    proposals = []
    for number, track in enumerate(album.ordered_tracks, 1):
        for key, value in (("musicbrainz_albumid", RELEASE), ("musicbrainz_releasegroupid", GROUP),
                           ("musicbrainz_albumartistid", ARTIST), ("musicbrainz_trackid", recording(number)),
                           ("title", f"Titre corrigé {number}"), ("date", "2001")):
            proposals.append(TagProposal(track.path, key, track.value(key), value, "test", url))
    return Verification(PROBABLE, [ReleaseCandidate(RELEASE, url=url, plausible=True)], proposals, ["ok"])


class FakeClient:
    def __init__(self, cache_path=None):
        self.closed = False

    def verify_album(self, album):
        if album.path.endswith("Incertain"):
            return Verification("Édition indéterminée", reasons=["Plusieurs éditions ont des pistes cohérentes."])
        return probable(album)

    def close(self):
        self.closed = True


def test_only_identifiers_are_kept(tmp_path):
    album = library(tmp_path, ("Sûr",))[0]
    album_values, track_values = identifiers(probable(album), album)
    assert album_values == {"musicbrainz_albumid": RELEASE, "musicbrainz_releasegroupid": GROUP,
                            "musicbrainz_albumartistid": ARTIST}
    assert {key for values in track_values.values() for key in values} == {"musicbrainz_trackid"}
    uncertain = Verification("Édition indéterminée", proposals=probable(album).proposals)
    assert identifiers(uncertain, album) == ({}, {})  # jamais rien pour un album incertain


def test_estimate_counts_requests_at_one_per_second(tmp_path):
    albums = library(tmp_path)
    assert estimate(albums) == (12.0, 12.0)            # recherche + cinq éditions par album
    tags = albums[0].tracks[0].tags
    for track in albums[0].tracks:
        track.tags["musicbrainz_albumid"] = [RELEASE]
    assert estimate(albums) == (7.0, 13.0) and tags  # MBID connu : une requête, sept au pire


def test_verification_can_be_cancelled_between_albums(tmp_path):
    albums = library(tmp_path)
    cancel = threading.Event()
    seen = []

    def progress(position, total, text):
        seen.append(position)
        if position == 1:
            cancel.set()
    with pytest.raises(ScanCancelled):
        verify_albums(albums, FakeClient(), cancel, progress)
    assert seen == [1]


def test_titles_and_dates_are_never_written(tmp_path):
    albums = library(tmp_path)
    results = verify_albums(albums, FakeClient())
    assert [result.writable for result in results] == [False, True]
    edits = identifier_edits(results)
    assert {key for edit in edits for key in edit.changes} == {
        "musicbrainz_albumid", "musicbrainz_releasegroupid", "musicbrainz_albumartistid", "musicbrainz_trackid"}
    assert all(edit.path.startswith(str(albums[1].path)) for edit in edits)
    assert identifier_edits(results, chosen=set()) == []


def test_real_client_proposes_the_release_track_identifier(tmp_path):
    from musicscope.models import Album, Track
    album = Album("/m/A", [Track("/m/A/1.flac", duration=120, tags={
        "album": ["Lumières"], "albumartist": ["Écho"], "artist": ["Écho"], "title": ["Le départ"],
        "tracknumber": ["1"], "discnumber": ["1"], "musicbrainz_albumid": [RELEASE]})])
    credit = [{"name": "Écho", "artist": {"id": ARTIST, "name": "Écho"}}]
    release = {"id": RELEASE, "title": "Lumières", "artist-credit": credit, "date": "2001", "country": "FR",
               "release-group": {"id": GROUP}, "media": [{"position": 1, "tracks": [{
                   "id": "66666666-6666-4666-8666-666666666666", "position": 1, "title": "Le départ",
                   "length": 120000, "recording": {"id": recording(1), "title": "Le départ",
                                                   "artist-credit": credit}}]}]}
    calls = []

    def opener(request, timeout):
        calls.append(request.full_url)
        return io.BytesIO(json.dumps(release).encode())
    with MusicBrainzClient(tmp_path / "mb.sqlite3", opener=opener, clock=time.monotonic,
                           sleep=lambda seconds: None) as client:
        verification = client.verify_album(album)
    assert verification.status == PROBABLE and len(calls) == 1
    _, track_values = identifiers(verification, album)
    assert track_values["/m/A/1.flac"]["musicbrainz_releasetrackid"] == "66666666-6666-4666-8666-666666666666"


def test_dialog_checks_only_writable_albums(tmp_path, app):
    from PySide6.QtCore import Qt

    from musicscope.mb_batch_gui import MusicBrainzBatchDialog
    results = verify_albums(library(tmp_path), FakeClient())
    dialog = MusicBrainzBatchDialog(results)
    try:
        assert dialog.chosen() == {results[1].album.path}
        uncertain = next(row for row in range(2) if dialog.table.item(row, 0).text() == "Incertain")
        assert not dialog.table.item(uncertain, 0).flags() & Qt.ItemFlag.ItemIsUserCheckable
        assert "rien n’est écrit" in dialog.table.item(uncertain, 1).text()
    finally:
        dialog.deleteLater()


def test_window_writes_identifiers_of_sure_albums_in_one_batch(tmp_path, app):
    from musicscope.advanced_gui import AdvancedWindow
    library(tmp_path)
    window = AdvancedWindow(tmp_path / "cache")
    try:
        window.root = str(tmp_path / "lib")
        window.start_scan()
        settle(window, app)
        albums = sorted(window.inventory.albums, key=lambda album: album.path)
        window.musicbrainz_batch(albums=albums, chosen=set(), confirm=True, client_factory=FakeClient)
        settle(window, app)
        assert set(window.verifications) == {album.path for album in albums}  # reprises par l'éditeur
        assert "Aucun album coché" in window.state.text()
        window.musicbrainz_batch(albums=albums, chosen={albums[1].path}, confirm=True, client_factory=FakeClient)
        settle(window, app)
        for path in sorted((tmp_path / "lib" / "Artiste" / "Sûr").glob("*.flac")):
            tags = _Tags(path)
            assert tags.get("musicbrainz_albumid") == [RELEASE] and tags.get("title")[0].startswith("Titre ")
            assert "corrigé" not in tags.get("title")[0] and tags.get("date") == []
        for path in (tmp_path / "lib" / "Artiste" / "Incertain").glob("*.flac"):
            assert _Tags(path).get("musicbrainz_albumid") == []
        with TagJournal(window.cache_dir / "tags.sqlite3") as journal:
            assert [batch.files for batch in journal.batches()] == [2]
    finally:
        window.close()


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
