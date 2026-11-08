import copy
import io
import json
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest

from musicscope.models import Album, Track
from musicscope.musicbrainz import MusicBrainzClient, USER_AGENT

RELEASE = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"
RECORDING = "33333333-3333-4333-8333-333333333333"
ARTIST = "44444444-4444-4444-8444-444444444444"
GROUP = "55555555-5555-4555-8555-555555555555"


def local(mbid=""):
    tags = {"album": ["Lumières"], "albumartist": ["Écho"], "artist": ["Écho"],
            "title": ["Le départ"], "tracknumber": ["1"], "discnumber": ["1"]}
    if mbid:
        tags["musicbrainz_albumid"] = [mbid]
    return Album("/musique/Écho/Lumières", [Track("/musique/Écho/Lumières/1.flac", duration=120, tags=tags)])


def remote(mbid=RELEASE, title="Le départ", duration=120000):
    credit = [{"name": "Écho", "artist": {"id": ARTIST, "name": "Écho"}}]
    return {"id": mbid, "title": "Lumières", "artist-credit": credit, "date": "2001-03-01", "country": "FR",
            "release-group": {"id": GROUP, "first-release-date": "1999"},
            "media": [{"position": 1, "tracks": [{"position": 1, "title": title, "length": duration,
                        "recording": {"id": RECORDING, "title": title, "artist-credit": credit}}]}]}


class Timer:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, amount):
        self.sleeps.append(amount)
        self.now += amount


class Responses:
    def __init__(self, *values, timer=None):
        self.values = list(values)
        self.requests = []
        self.times = []
        self.timer = timer

    def __call__(self, request, timeout):
        self.requests.append(request)
        self.times.append(self.timer.clock() if self.timer else 0)
        assert timeout == 20
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return io.BytesIO(json.dumps(value).encode())


def client(tmp_path, responses, timer=None, **kwargs):
    timer = timer or Timer()
    return MusicBrainzClient(tmp_path / "mb.sqlite", opener=responses,
                            clock=timer.clock, sleep=timer.sleep, **kwargs)


def test_direct_release_matching_tracks_proposes_tags_without_modification(tmp_path):
    album = local(RELEASE)
    before = copy.deepcopy(album)
    transport = Responses(remote())
    with client(tmp_path, transport) as mb:
        result = mb.verify_album(album)
    assert result.status == "Édition probable"
    assert result.candidates[0].plausible
    assert {p.field for p in result.proposals} >= {"originaldate", "date", "musicbrainz_trackid", "musicbrainz_releasegroupid"}
    assert all(p.url.endswith(RELEASE) for p in result.proposals)
    assert album == before
    assert len(transport.requests) == 1
    assert transport.requests[0].get_header("User-agent") == USER_AGENT
    assert parse_qs(urlparse(transport.requests[0].full_url).query)["inc"] == ["recordings+artist-credits+release-groups"]


def test_same_tracklists_on_multiple_editions_remain_ambiguous(tmp_path):
    transport = Responses({"releases": [{"id": RELEASE}, {"id": SECOND}], "release-count": 2}, remote(), remote(SECOND))
    with client(tmp_path, transport) as mb:
        result = mb.verify_album(local())
    assert result.status == "Édition indéterminée"
    assert len([c for c in result.candidates if c.plausible]) == 2
    assert result.proposals == []


def test_inconsistent_direct_release_falls_back_and_detects_correct_edition(tmp_path):
    transport = Responses(remote(title="Autre chanson"), {"releases": [{"id": SECOND}]}, remote(SECOND))
    with client(tmp_path, transport) as mb:
        result = mb.verify_album(local(RELEASE))
    assert result.status == "Édition probable"
    assert [c.id for c in result.candidates if c.plausible] == [SECOND]
    assert any("incohérent" in reason for reason in result.reasons)
    assert any(p.field == "musicbrainz_albumid" and p.proposed == SECOND for p in result.proposals)


def test_unavailable_distinct_from_no_result(tmp_path):
    transport = Responses(URLError("offline"), URLError("offline"), URLError("offline"))
    with client(tmp_path, transport) as mb:
        unavailable = mb.verify_album(local())
    assert unavailable.status == "Service indisponible"
    assert len(transport.requests) == 3
    with client(tmp_path, Responses({"releases": [], "release-count": 0})) as mb:
        empty = mb.verify_album(local())
    assert empty.status == "Aucun résultat"


def test_cache_prevents_requests_and_rate_limit_covers_search_and_lookup(tmp_path):
    timer = Timer()
    transport = Responses({"releases": [{"id": RELEASE}]}, remote(), timer=timer)
    with client(tmp_path, transport, timer) as mb:
        assert mb.verify_album(local()).status == "Édition probable"
        assert mb.verify_album(local()).status == "Édition probable"
    assert len(transport.requests) == 2
    assert transport.times[1] - transport.times[0] >= 1
    with client(tmp_path, Responses()) as mb:
        assert mb.verify_album(local()).status == "Édition probable"


def test_temporary_error_has_bounded_retry_and_retry_after(tmp_path):
    timer = Timer()
    failure = HTTPError("https://musicbrainz.org", 503, "busy", {"Retry-After": "2"}, None)
    transport = Responses(failure, remote(), timer=timer)
    with client(tmp_path, transport, timer) as mb:
        assert mb.verify_album(local(RELEASE)).status == "Édition probable"
    assert transport.times[1] - transport.times[0] >= 2
    failure = HTTPError("https://musicbrainz.org", 429, "busy", {"Retry-After": "600"}, None)
    transport = Responses(failure)
    with client(tmp_path / "second", transport) as mb:
        assert mb.verify_album(local(RELEASE)).status == "Service indisponible"
    assert len(transport.requests) == 1


def test_not_found_lookup_falls_back_and_search_escapes_lucene(tmp_path):
    transport = Responses(HTTPError("https://musicbrainz.org", 404, "missing", {}, None), {"releases": []})
    album = local(RELEASE)
    album.tracks[0].tags["album"] = ['A "B" + (C)']
    with client(tmp_path, transport) as mb:
        result = mb.verify_album(album)
    assert result.status == "Aucun résultat"
    query = parse_qs(urlparse(transport.requests[1].full_url).query)["query"][0]
    assert r'A \"B\" \+ \(C\)' in query


def test_truncated_search_never_selects_only_visible_matching_release(tmp_path):
    transport = Responses({"releases": [{"id": RELEASE}], "release-count": 120}, remote())
    with client(tmp_path, transport) as mb:
        result = mb.verify_album(local())
    assert result.status == "Édition indéterminée"
    assert not result.proposals


def test_search_count_field_also_prevents_truncated_selection(tmp_path):
    transport = Responses({"releases": [{"id": RELEASE}], "count": 120}, remote())
    with client(tmp_path, transport) as mb:
        result = mb.verify_album(local())
    assert result.status == "Édition indéterminée"
    assert not result.proposals


@pytest.mark.parametrize("mutate", [
    lambda album: setattr(album, "complete", False),
    lambda album: album.tracks[0].tags.update({"tracknumber": ["2"]}),
    lambda album: setattr(album.tracks[0], "duration", 140),
    lambda album: setattr(album.tracks[0], "duration", None),
    lambda album: album.tracks[0].tags.update({"musicbrainz_trackid": [SECOND]}),
])
def test_incomplete_or_conflicting_playlist_not_selected(tmp_path, mutate):
    album = local()
    mutate(album)
    with client(tmp_path, Responses({"releases": [{"id": RELEASE}]}, remote())) as mb:
        result = mb.verify_album(album)
    assert result.status == "Édition indéterminée"
    assert not result.proposals


def test_recording_id_can_support_missing_duration_but_not_conflicting_title(tmp_path):
    album = local(RELEASE)
    album.tracks[0].duration = None
    album.tracks[0].tags["musicbrainz_trackid"] = [RECORDING]
    with client(tmp_path, Responses(remote())) as mb:
        result = mb.verify_album(album)
    assert result.status == "Édition probable"


def test_invalid_payload_is_service_failure_not_empty_result(tmp_path):
    with client(tmp_path, Responses({"unexpected": "payload"})) as mb:
        result = mb.verify_album(local())
    assert result.status == "Service indisponible"


@pytest.mark.parametrize("payload", [
    {"releases": [None]},
    {"releases": [{"id": None}]},
])
def test_malformed_search_result_does_not_crash(tmp_path, payload):
    with client(tmp_path, Responses(payload)) as mb:
        result = mb.verify_album(local())
    assert result.status == "Service indisponible"


def test_malformed_release_does_not_crash(tmp_path):
    payload = remote()
    payload["media"] = [None]
    with client(tmp_path, Responses(payload)) as mb:
        result = mb.verify_album(local(RELEASE))
    assert result.status == "Service indisponible"


def test_partial_search_failure_does_not_claim_unique_edition(tmp_path):
    failure = HTTPError("https://musicbrainz.org", 403, "blocked", {}, None)
    transport = Responses({"releases": [{"id": RELEASE}, {"id": SECOND}]}, remote(), failure)
    with client(tmp_path, transport) as mb:
        result = mb.verify_album(local())
    assert result.status == "Service indisponible"
    assert len(result.candidates) == 1
    assert not result.proposals


def test_cache_expiration_requires_fresh_request(tmp_path):
    timer = Timer()
    transport = Responses(remote(), remote())
    with client(tmp_path, transport, timer, wall_clock=timer.clock, cache_ttl=1) as mb:
        assert mb.verify_album(local(RELEASE)).status == "Édition probable"
        timer.now += 2
        assert mb.verify_album(local(RELEASE)).status == "Édition probable"
    assert len(transport.requests) == 2


@pytest.mark.parametrize("extra", [
    {"tracknumber": ["1/12"]}, {"tracktotal": ["12"]},
    {"disctotal": ["2"]}, {"discnumber": ["1/2"]},
])
def test_known_missing_tracks_or_discs_never_selects_edition(tmp_path, extra):
    album = local()
    album.tracks[0].tags.update(extra)
    with client(tmp_path, Responses({"releases": [{"id": RELEASE}]}, remote())) as mb:
        result = mb.verify_album(album)
    assert result.status == "Édition indéterminée"
    assert not result.candidates[0].plausible
    assert any("totaux" in reason for reason in result.candidates[0].reasons)


@pytest.mark.parametrize("date", ["2001", "2001-03", "2001-03-01"])
def test_date_matches_at_available_precision(tmp_path, date):
    album = local(RELEASE)
    album.tracks[0].tags["date"] = [date]
    with client(tmp_path, Responses(remote())) as mb:
        result = mb.verify_album(album)
    assert result.status == "Édition probable"
    assert any("précision" in reason for reason in result.candidates[0].reasons)


def test_edition_date_disambiguates_identical_tracklists(tmp_path):
    album = local()
    album.tracks[0].tags["date"] = ["2001"]
    first, second = remote(), remote(SECOND)
    first["date"] = "1999-01-01"
    with client(tmp_path, Responses({"releases": [{"id": RELEASE}, {"id": SECOND}]}, first, second)) as mb:
        result = mb.verify_album(album)
    assert result.status == "Édition probable"
    assert [candidate.id for candidate in result.candidates if candidate.plausible] == [SECOND]
    rejected = next(candidate for candidate in result.candidates if candidate.id == RELEASE)
    assert any("Date d'édition différente" in reason for reason in rejected.reasons)


def test_country_disambiguates_identical_tracklists(tmp_path):
    album = local()
    album.tracks[0].tags["releasecountry"] = ["FR"]
    first, second = remote(), remote(SECOND)
    first["country"] = "US"
    with client(tmp_path, Responses({"releases": [{"id": RELEASE}, {"id": SECOND}]}, first, second)) as mb:
        result = mb.verify_album(album)
    assert result.status == "Édition probable"
    assert [candidate.id for candidate in result.candidates if candidate.plausible] == [SECOND]


def test_original_dates_may_vary_in_compilation(tmp_path):
    album = local(RELEASE)
    album.tracks[0].tags["originaldate"] = ["1990"]
    second = copy.deepcopy(album.tracks[0])
    second.path += "-second"
    second.tags["tracknumber"] = ["2"]
    second.tags["originaldate"] = ["1995"]
    album.tracks.append(second)
    release = remote()
    second_remote = copy.deepcopy(release["media"][0]["tracks"][0])
    second_remote["position"] = 2
    release["media"][0]["tracks"].append(second_remote)
    with client(tmp_path, Responses(release)) as mb:
        result = mb.verify_album(album)
    assert result.status == "Édition probable"
    assert any("compilation" in reason for reason in result.candidates[0].reasons)
    assert all("compilation" in proposal.reason for proposal in result.proposals
               if proposal.field == "originaldate")


def test_same_multi_albumartist_vector_is_coherent(tmp_path):
    album = local(RELEASE)
    album.tracks[0].tags["albumartist"] = ["Écho", "Invité"]
    release = remote()
    release["artist-credit"] = [{"name": "Invité", "joinphrase": " & ", "artist": {"name": "Invité"}},
                                {"name": "Écho", "artist": {"name": "Écho"}}]
    with client(tmp_path, Responses(release)) as mb:
        result = mb.verify_album(album)
    assert result.status == "Édition probable"


@pytest.mark.parametrize("mutate", [
    lambda data: data.update({"country": ["FR"]}),
    lambda data: data["release-group"].update({"first-release-date": 1999}),
    lambda data: data["artist-credit"][0]["artist"].update({"id": [ARTIST]}),
    lambda data: data["media"][0]["tracks"][0]["recording"].update({"id": {"bad": "id"}}),
    lambda data: data["media"][0]["tracks"][0].update({"recording": None}),
])
def test_nested_malformed_response_is_service_unavailable(tmp_path, mutate):
    payload = remote()
    mutate(payload)
    with client(tmp_path, Responses(payload)) as mb:
        result = mb.verify_album(local(RELEASE))
    assert result.status == "Service indisponible"
