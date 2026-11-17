"""qBittorrent (v0.13.0) : lecture des torrents et protection de leurs fichiers.

Le faux serveur suit la Web API v2 documentée (wiki « WebUI API (qBittorrent 5.0) » et
WebAPI_Changelog.md) : cookie SID après connexion, Referer obligatoire, 401 sur identifiants
refusés, clé API en ``Authorization: Bearer``, ``includeFiles`` sur torrents/info.
"""
import io
import json
import os
import shutil
import time
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

import musicscope
from musicscope.jellyfin import PathMapping
from musicscope.qbittorrent import QBittorrentClient, QBittorrentUnavailable, SeedIndex

TONE = Path(musicscope.__file__).parent / "data" / "tone-a.flac"
HASH = "a" * 40


class Response(io.BytesIO):
    def __init__(self, body, cookies=()):
        super().__init__(body if isinstance(body, bytes) else json.dumps(body).encode())
        self.headers = Message()
        for cookie in cookies:
            self.headers["Set-Cookie"] = cookie


class FakeServer:
    """WebUI qBittorrent minimale, en mémoire."""

    def __init__(self, torrents, password="secret", api_key="", include_files=True):
        self.torrents, self.password, self.api_key, self.include_files = torrents, password, api_key, include_files
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        url = urlsplit(request.full_url)
        assert request.get_header("Referer") == "http://qbit:8080"  # protection CSRF de qBittorrent
        if url.path == "/api/v2/auth/login":
            form = parse_qs(request.data.decode())
            if form.get("password") != [self.password]:
                raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)
            return Response(b"Ok.", ["SID=abc123; HttpOnly; path=/"])
        authorized = request.get_header("Cookie") == "SID=abc123" or (
            self.api_key and request.get_header("Authorization") == f"Bearer {self.api_key}")
        if not authorized:
            raise HTTPError(request.full_url, 403, "Forbidden", {}, None)
        query = parse_qs(url.query)
        if url.path == "/api/v2/app/version":
            return Response(b"v5.1.2")
        if url.path == "/api/v2/torrents/info":
            offset, limit = int(query["offset"][0]), int(query["limit"][0])
            page = [dict(torrent) for torrent in self.torrents[offset:offset + limit]]
            if not self.include_files:
                for torrent in page:
                    torrent.pop("files", None)
            return Response(page)
        if url.path == "/api/v2/torrents/files":
            torrent = next(item for item in self.torrents if item["hash"] == query["hash"][0])
            return Response(torrent["files"])
        raise HTTPError(request.full_url, 404, "Not Found", {}, None)


def torrent(name, save_path, files, state="stalledUP", number=0):
    return {"hash": f"{number:040x}" if number else HASH, "name": name, "state": state,
            "save_path": save_path, "content_path": f"{save_path}/{name}",
            "files": [{"name": f"{name}/{file}", "size": size} for file, size in files]}


def downloads(tmp_path):
    """Téléchargement qBittorrent, avec lien Lidarr dans la médiathèque."""
    album = tmp_path / "downloads" / "Artiste - Album (2020) [FLAC]"
    album.mkdir(parents=True)
    shutil.copy(TONE, album / "01.flac")
    library = tmp_path / "lib" / "Artiste" / "Album (2020)"
    library.mkdir(parents=True)
    os.link(album / "01.flac", library / "01 - Titre.flac")
    shutil.copy(TONE, library / "02 - Copie.flac")  # simple copie, sans lien
    return album, library


def test_login_cookie_and_seed_index_through_a_container_mapping(tmp_path):
    album, library = downloads(tmp_path)
    size = (album / "01.flac").stat().st_size
    server = FakeServer([torrent(album.name, "/downloads", [("01.flac", size)])])
    client = QBittorrentClient("http://qbit:8080", "admin", "secret", opener=server)
    index = client.seed_index([PathMapping("/downloads", str(tmp_path / "downloads"))])
    assert index.version == "v5.1.2" and index.torrents == 1 and len(index.files) == 1
    assert index.files[0].local_path == str(album / "01.flac") and index.files[0].active
    assert index.protected(str(album / "01.flac")) is not None                 # même chemin
    assert index.protected(str(library / "01 - Titre.flac")) is not None       # lien Lidarr : même inode
    assert index.protected(str(library / "02 - Copie.flac")) is None           # copie indépendante
    assert index.find(str(library / "02 - Copie.flac"))[1] == ""
    login = server.requests[0]
    assert login.get_method() == "POST" and urlsplit(login.full_url).path == "/api/v2/auth/login"
    assert all(request.get_method() == "GET" for request in server.requests[1:])  # aucune modification
    assert "secret" not in "".join(request.full_url for request in server.requests)


def test_wrong_password_is_reported_without_retry(tmp_path):
    server = FakeServer([], password="secret")
    client = QBittorrentClient("http://qbit:8080", "admin", "faux", opener=server)
    with pytest.raises(QBittorrentUnavailable, match="401"):
        client.seed_index([])
    assert len(server.requests) == 1  # jamais de nouvelle tentative : qBittorrent bannit après 5 échecs


def test_api_key_needs_no_login_and_old_webapi_without_include_files(tmp_path):
    album, _ = downloads(tmp_path)
    size = (album / "01.flac").stat().st_size
    server = FakeServer([torrent(album.name, str(tmp_path / "downloads"), [("01.flac", size)], "pausedUP")],
                        api_key="qbt_" + "x" * 28, include_files=False)
    client = QBittorrentClient("http://qbit:8080", api_key="qbt_" + "x" * 28, opener=server)
    index = client.seed_index([])
    assert not any(urlsplit(request.full_url).path == "/api/v2/auth/login" for request in server.requests)
    assert any(urlsplit(request.full_url).path == "/api/v2/torrents/files" for request in server.requests)
    paused = index.protected(str(album / "01.flac"))
    assert paused is not None and not paused.active  # en pause : protégé aussi


def test_torrent_list_is_paginated(tmp_path):
    torrents = [torrent(f"T{number}", "/x", [], number=number) for number in range(1, 8)]
    client = QBittorrentClient("http://qbit:8080", "admin", "secret", opener=FakeServer(torrents), page_size=3)
    assert len(client.torrents()) == 7


@pytest.mark.parametrize("url", ["ftp://qbit", "http://user:pass@qbit:8080", "http://qbit:8080/?x=1", "qbit"])
def test_invalid_addresses_are_refused(url):
    with pytest.raises(ValueError):
        QBittorrentClient(url, "admin", "secret")


def test_credentials_are_required():
    with pytest.raises(ValueError, match="clé API"):
        QBittorrentClient("http://qbit:8080")


def test_summary_counts_active_files():
    from musicscope.qbittorrent import TorrentFile
    index = SeedIndex([TorrentFile("A", "stalledUP", "/d/a", "/l/a", 1), TorrentFile("B", "pausedUP", "/d/b", None, 1)],
                      torrents=2, unmapped=1, version="v5.0")
    assert "2 torrent(s)" in index.summary() and "dont 1 en partage actif" in index.summary()
    assert "1 chemin(s) sans correspondance" in index.summary()


# Interface
@pytest.fixture
def window(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from musicscope.advanced_gui import AdvancedWindow
    app = QApplication.instance() or QApplication([])
    album, library = downloads(tmp_path)
    shutil.copy(TONE, library.parent / "seule.flac")  # pour un second album indépendant
    window = AdvancedWindow(tmp_path / "cache")
    window.root = str(tmp_path / "lib")
    size = (album / "01.flac").stat().st_size
    server = FakeServer([torrent(album.name, "/downloads", [("01.flac", size)])])
    window.start_scan()
    settle(window, app)
    window.connect_qbittorrent({"server": "http://qbit:8080", "username": "admin", "password": "secret",
                                "mappings": [("/downloads", str(tmp_path / "downloads"))]},
                               client_factory=lambda *args: QBittorrentClient(*args, opener=server))
    settle(window, app)
    yield window, app, album, library
    window.close()


def settle(window, app, timeout=30):
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


def test_preferences_never_store_password_or_key(window):
    window, app, album, library = window
    assert window.seed_index is not None and len(window.seed_index.files) == 1
    saved = json.loads((window.cache_dir / "preferences.json").read_text("utf-8"))["qbittorrent"]
    assert saved == {"server": "http://qbit:8080", "username": "admin",
                     "mappings": [[ "/downloads", str(album.parent)]]}
    assert "secret" not in (window.cache_dir / "preferences.json").read_text("utf-8")
    assert "protégés" in window.state.text()


def test_torrent_files_block_organizer_and_tag_editor(window):
    window, app, album, library = window
    row = next(index for index, item in enumerate(window.inventory.albums) if item.path == str(library))
    window.album_table.selectRow(window.album_proxy.mapFromSource(window.album_model.index(row, 0)).row())
    app.processEvents()
    window.preview_organization(destination_root=str(library.parent.parent))
    settle(window, app)
    assert any("torrent qBittorrent" in blocker for blocker in window.pending_plan.blockers)
    before = (library / "01 - Titre.flac").read_bytes()
    window.edit_tags(dialog_values=({"genre": "Jazz"}, {}), confirm=True)
    settle(window, app)
    assert (library / "01 - Titre.flac").read_bytes() == before


def test_torrent_files_are_never_selected_nor_quarantined(window):
    from musicscope import file_duplicates as fd
    window, app, album, library = window
    page = window.file_duplicates_page
    page.min_size.setValue(0)
    for box in page.criteria_boxes.values():
        box.setChecked(False)
    page.criteria_boxes[fd.CONTENT].setChecked(True)
    page.search()
    settle(window, app)
    seeded = [record for group in page.groups for record in group.files if record.seeded]
    assert seeded and all(record.path not in fd.select_all_but_best(page.groups) for record in seeded)
    page.apply_selection({record.path for record in seeded})
    page.quarantine(confirm=True)
    settle(window, app)
    assert "torrent qBittorrent" in window.state.text()
    assert all(Path(record.path).exists() for record in seeded)
