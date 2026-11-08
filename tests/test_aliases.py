import io
import json
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from musicscope.aliases import ArtistAliasResolver

ARTIST = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"


class Responses:
    def __init__(self, *items):
        self.items = list(items)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return io.BytesIO(json.dumps(item).encode())


def resolver(tmp_path, opener):
    return ArtistAliasResolver(tmp_path / "aliases.sqlite", opener, clock=lambda: 100, sleep=lambda _: None)


def artist(mbid=ARTIST):
    return {"id": mbid, "name": "東京", "aliases": [{"name": "Tokyo"}], "disambiguation": "artiste japonais"}


def test_sourced_alias_remains_manual_and_cache_reused(tmp_path):
    opener = Responses({"artists": [{"id": ARTIST}], "count": 1}, artist())
    client = resolver(tmp_path, opener)
    try:
        result = client.resolve("Tokyo")
        assert result.status == "Alias probable à valider"
        assert result.candidates[0].name == "東京"
        assert result.candidates[0].aliases == ["Tokyo"]
        assert result.candidates[0].url.endswith(ARTIST)
        assert "sélection explicite" in result.reasons[0]
        assert client.resolve("Tokyo").candidates == result.candidates
        assert len(opener.requests) == 2
        assert "alias:" in parse_qs(urlsplit(opener.requests[0].full_url).query)["query"][0]
    finally:
        client.close()


def test_homonyms_and_truncated_results_remain_undetermined(tmp_path):
    opener = Responses({"artists": [{"id": ARTIST}, {"id": SECOND}], "count": 2}, artist(), artist(SECOND))
    client = resolver(tmp_path, opener)
    try:
        assert client.resolve("Tokyo").status == "Artiste indéterminé"
    finally:
        client.close()
    opener = Responses({"artists": [{"id": ARTIST}], "count": 20}, artist())
    client = ArtistAliasResolver(tmp_path / "second.sqlite", opener, clock=lambda: 100, sleep=lambda _: None)
    try:
        assert client.resolve("Tokyo").status == "Artiste indéterminé"
    finally:
        client.close()


def test_alias_absence_distinct_from_offline(tmp_path):
    client = resolver(tmp_path, Responses({"artists": [], "count": 0}))
    try:
        assert client.resolve("Sans résultat").status == "Aucun résultat"
    finally:
        client.close()
    client = resolver(tmp_path, Responses(URLError("offline"), URLError("offline"), URLError("offline")))
    try:
        assert client.resolve("Indisponible").status == "Service indisponible"
    finally:
        client.close()


def test_alias_malformed_payload_is_unavailable(tmp_path):
    client = resolver(tmp_path, Responses({"artists": [{"id": ARTIST}]}, {"id": ARTIST, "name": "Tokyo", "aliases": [None]}))
    try:
        assert client.resolve("Tokyo").status == "Service indisponible"
    finally:
        client.close()
