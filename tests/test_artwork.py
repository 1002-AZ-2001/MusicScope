import hashlib
import io
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest

from musicscope.artwork import ArtworkClient, _safe_url, compare_artworks
from musicscope.musicbrainz import ServiceUnavailable

RELEASE = "11111111-1111-4111-8111-111111111111"
PNG = b"\x89PNG\r\n\x1a\nsynthetic-test-image"


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr("musicscope.recognition.time.sleep", lambda seconds: None)


class Responses:
    def __init__(self, *items):
        self.items = list(items)
        self.urls = []

    def __call__(self, request, timeout):
        self.urls.append(request.full_url)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return io.BytesIO(item if isinstance(item, bytes) else json.dumps(item).encode())


def metadata(url="http://archive.org/download/mbid-test/front.png"):
    return {"images": [{"id": 123, "front": True, "image": url,
                        "thumbnails": {"500": url}},
                       {"id": 456, "front": False, "image": "https://archive.org/back.jpg"}]}


def test_cover_front_https_cache_and_comparison(tmp_path):
    opener = Responses(metadata(), PNG)
    client = ArtworkClient(tmp_path, opener)
    result = client.fetch(RELEASE)
    assert len(result) == 1 and result[0].front
    assert result[0].release_id == RELEASE
    assert result[0].source_url.startswith("https://archive.org/")
    assert Path(result[0].image_path).read_bytes() == PNG
    assert result[0].checksum == hashlib.sha256(PNG).hexdigest()
    assert client.fetch(RELEASE) == result
    assert len(opener.urls) == 2
    assert "ne prouve pas" in compare_artworks(result[0], result[0])


def test_cover_missing_is_empty_not_unavailable(tmp_path):
    opener = Responses(HTTPError("https://coverartarchive.org", 404, "not found", {}, None))
    assert ArtworkClient(tmp_path, opener).fetch(RELEASE) == []


@pytest.mark.parametrize("url", ["https://archive.org.evil.test/image", "file:///etc/passwd", "https://evil.test/image",
                                "https://user:secret@archive.org/image", "https://archive.org:444/image"])
def test_cover_rejects_untrusted_image_destinations(tmp_path, url):
    opener = Responses(metadata(url))
    with pytest.raises(ServiceUnavailable):
        ArtworkClient(tmp_path, opener).fetch(RELEASE)
    assert len(opener.urls) == 1


def test_cover_rejects_html_payload_and_invalid_release(tmp_path):
    with pytest.raises(ServiceUnavailable, match="PNG ou JPEG"):
        ArtworkClient(tmp_path, Responses(metadata(), b"<html>not image</html>")).fetch(RELEASE)
    with pytest.raises(ValueError, match="MBID"):
        ArtworkClient(tmp_path).fetch("../../outside")


def test_cover_cache_corruption_triggers_refresh(tmp_path):
    opener = Responses(metadata(), PNG, metadata(), PNG)
    client = ArtworkClient(tmp_path, opener)
    item = client.fetch(RELEASE)[0]
    Path(item.image_path).write_bytes(b"corrupt")
    assert client.fetch(RELEASE)[0].checksum == hashlib.sha256(PNG).hexdigest()
    assert len(opener.urls) == 4


def test_cover_redirect_validates_destination_before_request():
    from musicscope.artwork import _ArtworkRedirect
    with pytest.raises(ServiceUnavailable):
        _ArtworkRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.test/image")
