import io
import json
from pathlib import Path
from threading import Event
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

import pytest

from musicscope.cache import ScanCancelled
from musicscope.models import Track
from musicscope.musicbrainz import ServiceUnavailable
from musicscope.recognition import AcoustIDClient, FeatureUnavailable, Fingerprint, Fingerprinter, _read_url

RECORDING = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr("musicscope.recognition.time.sleep", lambda seconds: None)


def track(tmp_path):
    path = tmp_path / "source.flac"
    path.write_bytes("source inchangée".encode())
    info = path.stat()
    return Track(str(path), size=info.st_size, mtime_ns=info.st_mtime_ns)


def test_fpcalc_stream_command_and_cache_reuse(tmp_path, monkeypatch):
    calls = []
    local = track(tmp_path)
    monkeypatch.setattr("musicscope.recognition.shutil.which", lambda name: "/tools/fpcalc")

    def run(self, arguments, cancel):
        calls.append(arguments)
        if arguments[-1] == "-version":
            return "fpcalc version 1.5.1 (Chromaprint 1.5.1)"
        assert arguments == ["/tools/fpcalc", "-json", "-length", "120", local.path]
        return json.dumps({"duration": 123, "fingerprint": "AQADAA-fingerprint"})

    monkeypatch.setattr(Fingerprinter, "_run", run)
    client = Fingerprinter(tmp_path / "cache.sqlite")
    try:
        assert client.fingerprint(local) == Fingerprint(123, "AQADAA-fingerprint")
        assert client.fingerprint(local).fingerprint == "AQADAA-fingerprint"
        assert len(calls) == 2
        assert Path(local.path).read_bytes() == "source inchangée".encode()
    finally:
        client.close()


def test_fpcalc_missing_changed_and_cancelled(tmp_path, monkeypatch):
    local = track(tmp_path)
    monkeypatch.setattr("musicscope.recognition.shutil.which", lambda name: None)
    client = Fingerprinter(tmp_path / "cache.sqlite", fpcalc="missing-fpcalc")
    try:
        with pytest.raises(FeatureUnavailable, match="absent"):
            client.fingerprint(local)
        cancel = Event()
        cancel.set()
        with pytest.raises(ScanCancelled):
            client.fingerprint(local, cancel)
        Path(local.path).write_bytes(b"different")
        with pytest.raises(OSError, match="modifié"):
            client.fingerprint(local)
    finally:
        client.close()


def test_fpcalc_mutation_during_read_not_cached(tmp_path, monkeypatch):
    local = track(tmp_path)
    monkeypatch.setattr(Fingerprinter, "_find_executable", lambda *args: "fpcalc")

    def mutate(*args):
        Path(local.path).write_bytes(b"changed")
        return '{"duration": 123, "fingerprint": "AQADAA"}'

    monkeypatch.setattr(Fingerprinter, "_run", mutate)
    client = Fingerprinter(tmp_path / "cache.sqlite")
    try:
        with pytest.raises(OSError, match="modifié"):
            client.fingerprint(local)
        assert client.db.execute("SELECT COUNT(*) FROM fingerprints").fetchone()[0] == 0
    finally:
        client.close()


@pytest.mark.parametrize("value", ['{"duration":0,"fingerprint":"X"}', '{"duration":1,"fingerprint":""}', 'invalid json'])
def test_fpcalc_invalid_output(tmp_path, monkeypatch, value):
    local = track(tmp_path)
    monkeypatch.setattr(Fingerprinter, "_find_executable", lambda *args: "fpcalc")
    monkeypatch.setattr(Fingerprinter, "_run", lambda *args: value)
    client = Fingerprinter(tmp_path / "cache.sqlite")
    try:
        with pytest.raises(FeatureUnavailable, match="illisible"):
            client.fingerprint(local)
    finally:
        client.close()


class Responses:
    def __init__(self, *values):
        self.values = list(values)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        assert request.get_method() == "GET"
        assert timeout == 20
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())


def response(*ids):
    return {"status": "ok", "results": [{"score": 0.95, "recordings": [
        {"id": value, "title": "Chanson", "artists": [{"name": "Écho"}]} for value in ids]}]}


def test_acoustid_multiple_candidates_and_private_cache(tmp_path):
    opener = Responses(response(RECORDING, SECOND))
    client = AcoustIDClient("application-secret", tmp_path / "lookup.sqlite", opener)
    try:
        result = client.lookup(Fingerprint(123.4, "AQADAA"))
        assert len(result) == 2  # aucune édition choisie à partir d'une empreinte
        assert result[0].title == "Chanson"
        assert result[0].url == f"https://musicbrainz.org/recording/{RECORDING}"
        assert parse_qs(urlsplit(opener.requests[0].full_url).query)["client"] == ["application-secret"]
        assert len(client.lookup(Fingerprint(123.4, "AQADAA"))) == 2
        assert len(opener.requests) == 1
        bodies = list(client.db.execute("SELECT key,body FROM acoustid"))
        assert "application-secret" not in str(bodies)
    finally:
        client.close()


def test_acoustid_no_result_is_not_unavailability(tmp_path):
    client = AcoustIDClient("key", opener=Responses({"status": "ok", "results": []}))
    assert client.lookup(Fingerprint(120, "AQADAA")) == []
    client = AcoustIDClient("key", opener=Responses(URLError("private-key-never-displayed"), URLError("offline"), URLError("offline")))
    with pytest.raises(ServiceUnavailable) as error:
        client.lookup(Fingerprint(120, "AQADAA"))
    assert "private-key" not in str(error.value)


@pytest.mark.parametrize("payload", [[], {"status": "error"}, {"status": "ok", "results": [None]},
                                      {"status": "ok", "results": [{"score": float("nan")}]},
                                      {"status": "ok", "results": [{"recordings": [{"id": "bad"}]}]}])
def test_acoustid_invalid_payload(payload):
    client = AcoustIDClient("key", opener=Responses(payload))
    with pytest.raises(ServiceUnavailable):
        client.lookup(Fingerprint(120, "AQADAA"))


def test_read_url_bounded_retries_and_response_size():
    temporary = HTTPError("https://api.acoustid.org", 503, "temporary", {"Retry-After": "1"}, None)
    opener = Responses(temporary, response(RECORDING))
    assert _read_url("https://api.acoustid.org", opener)
    assert len(opener.requests) == 2
    with pytest.raises(ServiceUnavailable, match="volumineuse"):
        _read_url("https://api.acoustid.org", Responses(b"012345"), max_bytes=3)


def test_read_url_respects_long_retry_after():
    opener = Responses(HTTPError("https://api.acoustid.org", 429, "limit", {"Retry-After": "600"}, None))
    with pytest.raises(ServiceUnavailable, match="plus tard"):
        _read_url("https://api.acoustid.org", opener)
    assert len(opener.requests) == 1


def test_read_url_respects_retry_after_http_date():
    opener = Responses(HTTPError("https://api.acoustid.org", 429, "limit",
                                 {"Retry-After": "Tue, 31 Dec 2099 23:59:59 GMT"}, None))
    with pytest.raises(ServiceUnavailable, match="plus tard"):
        _read_url("https://api.acoustid.org", opener)
    assert len(opener.requests) == 1


def test_request_queue_spaces_requests(monkeypatch):
    from musicscope.recognition import _RequestQueue
    from urllib.request import Request
    now, sleeps = [100.0], []
    monkeypatch.setattr("musicscope.recognition.time.monotonic", lambda: now[0])

    def sleep(amount):
        sleeps.append(amount)
        now[0] += amount

    monkeypatch.setattr("musicscope.recognition.time.sleep", sleep)
    queue = _RequestQueue()
    opener = Responses(b"first", b"second")
    queue.request(Request("https://api.acoustid.org"), opener, 100)
    queue.request(Request("https://api.acoustid.org"), opener, 100)
    assert sleeps == [1.0]
