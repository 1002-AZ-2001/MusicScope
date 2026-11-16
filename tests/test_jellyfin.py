import io
import json
from pathlib import Path
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from musicscope.cache import ScanCancelled
from musicscope.jellyfin import (
    Diagnosis, JellyfinClient, JellyfinUnavailable, PathMapping,
    _SameOriginRedirect, map_server_path,
)
from musicscope.models import Album, Inventory, Track


class Responses:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        assert timeout == 20
        # La dernière réponse se répète : relances et relecture finale du total la retrouvent.
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return io.BytesIO(json.dumps(response).encode())


@pytest.fixture(autouse=True)
def no_retry_delay(monkeypatch):
    monkeypatch.setattr("musicscope.jellyfin.time.sleep", lambda seconds: None)


def item(number, path, **fields):
    return {"Id": str(number), "Path": path, "Name": f"Piste {number}", **fields}


def inventory(root, names=("a.flac", "b.mp3")):
    root.mkdir(exist_ok=True)
    tracks = []
    for name in names:
        path = root / name
        path.write_bytes(b"audio")
        tracks.append(Track(str(path)))
    return Inventory(str(root), [Album(str(root), tracks)])


def test_paginated_get_diagnostic_and_explicit_token_header(tmp_path):
    inv = inventory(tmp_path / "local")
    responses = Responses({"Items": [item(1, "/media/Musique/a.flac")], "TotalRecordCount": 2},
                          {"Items": [item(2, "/media/Musique/missing.flac")], "TotalRecordCount": 2})
    result = JellyfinClient("https://jellyfin.example/base/", "secret-test", opener=responses, page_size=1).diagnose(
        inv, [PathMapping("/media/Musique", inv.root)])
    assert result.complete
    assert [f.status for f in result.findings] == ["Correspondance", "Fichier local absent", "Absent du catalogue"]
    assert result.findings[0].local_path == str(tmp_path / "local" / "a.flac")
    assert len(responses.requests) == 3  # deux pages, puis relecture du total
    for index, request in enumerate(responses.requests):
        assert request.get_method() == "GET"
        assert 'Token="secret-test"' in request.get_header("Authorization")
        assert request.get_header("Authorization").startswith("MediaBrowser ")
        assert request.get_header("X-emby-token") is None
        assert "secret-test" not in request.full_url
        assert urlsplit(request.full_url).path == "/base/Items"
        query = parse_qs(urlsplit(request.full_url).query)
        assert query["StartIndex"] == [str(index % 2)]
        assert query["SortBy"] == ["DateCreated,SortName"] and query["SortOrder"] == ["Ascending"]


def test_missing_mapping_stays_visible_and_prevents_false_absence(tmp_path):
    inv = inventory(tmp_path / "local")
    responses = Responses({"Items": [item(1, "/unconfigured/a.flac")], "TotalRecordCount": 1})
    result = JellyfinClient("http://nas:8096", "key", opener=responses).diagnose(inv, [])
    assert result.complete
    assert result.findings[0].status == "Chemin non résolu"
    assert all(f.status != "Absent du catalogue" for f in result.findings)
    assert result.undetermined == 2


def test_mapping_longest_prefix_boundaries_and_root(tmp_path):
    general, specific = tmp_path / "general", tmp_path / "specific"
    mappings = [PathMapping("/media", str(general)), PathMapping("/media/Musique", str(specific))]
    assert map_server_path("/media/Musique/Artist/a.flac", mappings)[0] == str(specific / "Artist" / "a.flac")
    assert map_server_path("/mediator/a.flac", mappings)[0] is None
    assert map_server_path("/media2/a.flac", [PathMapping("/", str(general))])[0] == str(general / "media2" / "a.flac")


def test_windows_unc_and_drive_mapping_is_case_insensitive(tmp_path):
    mappings = [PathMapping(r"\\TRUENAS\Musique", str(tmp_path)), PathMapping(r"M:\Audio", str(tmp_path / "audio"))]
    assert map_server_path(r"\\truenas\musique\Artiste\a.flac", mappings)[0] == str(tmp_path / "Artiste" / "a.flac")
    assert map_server_path(r"m:\audio\Artiste\a.flac", mappings)[0] == str(tmp_path / "audio" / "Artiste" / "a.flac")
    assert map_server_path(r"\\truenas\musique-autre\a.flac", mappings)[0] is None


def test_mapping_ambiguity_and_traversal_are_not_hidden(tmp_path):
    mappings = [PathMapping("/music", str(tmp_path / "a")), PathMapping("/music", str(tmp_path / "b"))]
    target, reason = map_server_path("/music/a.flac", mappings)
    assert target is None and "Plusieurs" in reason
    assert map_server_path("/music/../private/a.flac", mappings)[0] is None
    assert map_server_path("relative/a.flac", mappings)[0] is None
    assert map_server_path("/music/a.flac", [PathMapping("/music", "relative")])[0] is None


def test_duplicate_catalogue_paths_are_separate_rows(tmp_path):
    inv = inventory(tmp_path / "local", ("a.flac",))
    responses = Responses({"Items": [item(1, "/music/a.flac"), item(2, "/music/a.flac")], "TotalRecordCount": 2})
    result = JellyfinClient("https://example.test", "key", opener=responses).diagnose(inv, [PathMapping("/music", inv.root)])
    assert [f.status for f in result.findings] == ["Correspondance", "Entrée dupliquée"]
    assert result.findings[0].item_id != result.findings[1].item_id


def test_removed_file_and_unavailable_root_are_distinct(tmp_path):
    inv = inventory(tmp_path / "local", ("a.flac",))
    Path(inv.albums[0].tracks[0].path).unlink()
    responses = Responses({"Items": [item(1, "/music/a.flac")], "TotalRecordCount": 1})
    result = JellyfinClient("https://example.test", "key", opener=responses).diagnose(inv, [PathMapping("/music", inv.root)])
    assert [finding.status for finding in result.findings] == ["Fichier local absent"]
    Path(inv.root).rmdir()
    responses = Responses({"Items": [item(1, "/music/a.flac")], "TotalRecordCount": 1})
    result = JellyfinClient("https://example.test", "key", opener=responses).diagnose(inv, [PathMapping("/music", inv.root)])
    assert [finding.status for finding in result.findings] == ["Accès local indéterminé"]


@pytest.mark.parametrize("failure", [URLError("offline"), HTTPError("url", 401, "Unauthorized", {}, None)])
def test_unavailable_service_cannot_claim_absent_catalogue(tmp_path, failure):
    inv = inventory(tmp_path / "local")
    result = JellyfinClient("https://example.test", "key", opener=Responses(failure)).diagnose(
        inv, [PathMapping("/music", inv.root)])
    assert not result.complete and result.failed
    assert result.reasons and result.findings == [] and result.undetermined == 0


def test_partial_catalogue_preserves_successful_page_without_false_absence(tmp_path):
    inv = inventory(tmp_path / "local")
    responses = Responses({"Items": [item(1, "/music/a.flac")], "TotalRecordCount": 2}, URLError("disconnected"))
    result = JellyfinClient("https://example.test", "key", opener=responses, page_size=1).diagnose(
        inv, [PathMapping("/music", inv.root)])
    assert not result.complete and not result.failed
    assert result.findings[0].status == "Correspondance"
    assert result.undetermined == 1 and len(result.findings) == 1
    assert any("non rapproché" in reason for reason in result.reasons)


@pytest.mark.parametrize("pages", [
    [{"Items": [] , "TotalRecordCount": 2}],
    [{"Items": [item(1, "/music/a.flac")], "TotalRecordCount": 3},
     {"Items": [item(1, "/music/a.flac")], "TotalRecordCount": 3}],
    [{"Items": [item(1, "/music/a.flac")], "TotalRecordCount": 3},
     {"Items": [item(2, "/music/b.mp3")], "TotalRecordCount": 4}],
])
def test_truncated_repeated_or_changed_pagination_is_incomplete(tmp_path, pages):
    inv = inventory(tmp_path / "local")
    result = JellyfinClient("https://example.test", "key", opener=Responses(*pages), page_size=1).diagnose(
        inv, [PathMapping("/music", inv.root)])
    assert not result.complete
    assert not any(f.status == "Absent du catalogue" for f in result.findings)


def test_limit_without_total_and_valid_empty_catalogue(tmp_path):
    inv = inventory(tmp_path / "local")
    client = JellyfinClient("https://example.test", "key", opener=Responses({"Items": []}))
    result = client.diagnose(inv, [])
    assert result.complete
    assert {f.status for f in result.findings} == {"Absent du catalogue"}
    client = JellyfinClient("https://example.test", "key", page_size=1, max_pages=1,
                            opener=Responses({"Items": [item(1, "/music/a.flac")]}))
    result = client.diagnose(inv, [PathMapping("/music", inv.root)])
    assert not result.complete
    assert any("Limite" in r for r in result.reasons)


@pytest.mark.parametrize("bad", [{}, {"Items": None}, {"Items": [None]},
                                  {"Items": [], "TotalRecordCount": "2"},
                                  {"Items": [item(1, None)]}])
def test_invalid_response_is_service_failure_not_missing_catalogue(tmp_path, bad):
    inv = inventory(tmp_path / "local")
    result = JellyfinClient("https://example.test", "key", opener=Responses(bad)).diagnose(inv, [])
    assert not result.complete
    assert not any(f.status == "Absent du catalogue" for f in result.findings)


@pytest.mark.parametrize("url", ["file:///private", "https://user:password@example.test", "https://example.test?key=secret", "https://example.test/#fragment"])
def test_invalid_url_is_rejected_without_echoing_credentials(url):
    with pytest.raises(ValueError) as error:
        JellyfinClient(url, "key")
    assert "password" not in str(error.value)


def test_cancel_propagates(tmp_path):
    inv = inventory(tmp_path / "local")
    cancel = Event()
    cancel.set()
    with pytest.raises(ScanCancelled):
        JellyfinClient("https://example.test", "key", opener=Responses()).diagnose(inv, [], cancel)


def test_offsite_redirect_cannot_forward_token():
    from urllib.request import Request
    handler = _SameOriginRedirect()
    request = Request("https://nas.example/Items", headers={"X-Emby-Token": "secret"})
    with pytest.raises(JellyfinUnavailable):
        handler.redirect_request(request, None, 302, "Found", {}, "https://attacker.example/Items")



def test_server_info_validates_key_and_lists_music_libraries():
    responses = Responses({"ServerName": "TrueNAS", "Version": "12.0.1"},
                          [{"Name": "Musique", "ItemId": "abc-123", "CollectionType": "music",
                            "Locations": ["/media/Musique"]},
                           {"Name": "Films", "ItemId": "def", "CollectionType": "movies", "Locations": ["/media/Films"]}],
                          [{"Name": "Musique", "ItemId": "abc-123", "CollectionType": "music",
                            "Locations": ["/media/Musique", "/media/Autres"]}])
    client = JellyfinClient("http://nas:8096/jellyfin", "key", opener=responses)
    info = client.server_info()
    assert (info.name, info.version) == ("TrueNAS", "12.0.1")
    assert responses.requests[0].get_header("Authorization") is None  # informations publiques sans clé
    assert urlsplit(responses.requests[0].full_url).path == "/jellyfin/System/Info/Public"
    assert 'Token="key"' in responses.requests[1].get_header("Authorization")
    libraries = client.libraries()
    assert [library.name for library in libraries if library.is_music] == ["Musique"]
    assert libraries[0].locations == ("/media/Musique", "/media/Autres")


@pytest.mark.parametrize("code,hint", [(401, "Clé API refusée"), (403, "droits"), (404, "sous-chemin"), (500, "HTTP 500")])
def test_http_errors_are_explained(tmp_path, code, hint):
    inv = inventory(tmp_path / "local")
    result = JellyfinClient("https://example.test", "key", opener=Responses(HTTPError("u", code, "x", {}, None))).diagnose(inv, [])
    assert result.failed and hint in result.reasons[0]
    with pytest.raises(JellyfinUnavailable) as error:
        JellyfinClient("https://example.test", "key", opener=Responses(HTTPError("u", code, "x", {}, None))).libraries()
    assert hint in str(error.value)


def test_library_scope_is_sent_as_parent_id(tmp_path):
    inv = inventory(tmp_path / "local")
    responses = Responses({"Items": [], "TotalRecordCount": 0})
    JellyfinClient("https://example.test", "key", opener=responses, library_id="abc-123").diagnose(inv, [])
    assert parse_qs(urlsplit(responses.requests[0].full_url).query)["ParentId"] == ["abc-123"]
    with pytest.raises(ValueError):
        JellyfinClient("https://example.test", "key", library_id="../x")
    with pytest.raises(ValueError):
        JellyfinClient("https://example.test", 'cle"injectee')


def test_key_never_in_url_or_errors(tmp_path):
    responses = Responses(HTTPError("u", 401, "x", {}, None))
    with pytest.raises(JellyfinUnavailable) as error:
        JellyfinClient("https://example.test", "tres-secret", opener=responses).libraries()
    assert "tres-secret" not in str(error.value) and "tres-secret" not in responses.requests[0].full_url


def test_one_transient_failure_is_retried(tmp_path):
    from socket import timeout as socket_timeout
    inv = inventory(tmp_path / "local")
    responses = Responses({"Items": [item(1, "/music/a.flac")], "TotalRecordCount": 2}, socket_timeout("lent"),
                          {"Items": [item(2, "/music/b.mp3")], "TotalRecordCount": 2})
    result = JellyfinClient("https://example.test", "key", opener=responses, page_size=1).diagnose(
        inv, [PathMapping("/music", inv.root)])
    assert result.complete and not result.failed
    assert [finding.status for finding in result.findings] == ["Correspondance", "Correspondance"]


def test_truncated_response_is_a_service_error(tmp_path):
    from http.client import IncompleteRead
    inv = inventory(tmp_path / "local")
    result = JellyfinClient("https://example.test", "key", opener=Responses(IncompleteRead(b"{"))).diagnose(inv, [])
    assert result.failed and "Connexion Jellyfin impossible" in result.reasons[0]


def test_catalogue_changed_during_paging_prevents_absence(tmp_path):
    inv = inventory(tmp_path / "local")
    responses = Responses({"Items": [item(1, "/music/a.flac")], "TotalRecordCount": 2},
                          {"Items": [item(3, "/music/c.flac")], "TotalRecordCount": 2},
                          {"Items": [item(1, "/music/a.flac")], "TotalRecordCount": 3})
    result = JellyfinClient("https://example.test", "key", opener=responses, page_size=1).diagnose(
        inv, [PathMapping("/music", inv.root)])
    assert not result.complete
    assert "Absent du catalogue" not in [finding.status for finding in result.findings]
    assert any("a changé pendant la lecture" in reason for reason in result.reasons)


@pytest.mark.parametrize("key", ["clé€", "abc\u00a0def", "a b"])
def test_api_key_with_unexpected_characters_is_rejected_early(key):
    with pytest.raises(ValueError, match="Recopiez la clé"):
        JellyfinClient("https://example.test", key)


def test_outgoing_symlink_is_ignored_without_blocking_the_diagnosis(tmp_path):
    outside = tmp_path / "ailleurs.flac"
    outside.write_bytes(b"audio")
    inv = inventory(tmp_path / "local", ("a.flac",))
    try:
        (tmp_path / "local" / "lien.flac").symlink_to(outside)
    except OSError:
        pytest.skip("liens symboliques indisponibles")
    target, reason = map_server_path("/music/lien.flac", [PathMapping("/music", inv.root)])
    assert target == str(tmp_path / "local" / "lien.flac")
    target, reason = map_server_path("/music/../ailleurs.flac", [PathMapping("/music", inv.root)])
    assert target is None
