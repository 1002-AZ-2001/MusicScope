"""Lidarr et Jellyfin prévenus après une modification (v0.16.0)."""
import io
import json
import os
import time
from urllib.error import HTTPError
from urllib.parse import urlsplit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from musicscope.jellyfin import JellyfinClient, PathMapping
from musicscope.lidarr import LidarrClient, LidarrUnavailable
from musicscope.notify import ServiceNotifier, to_server

KEY = "0123456789abcdef0123456789abcdef"


class Recorder:
    def __init__(self, responses=None, error=None):
        self.requests, self.responses, self.error = [], list(responses or []), error

    def __call__(self, request, timeout):
        self.requests.append(request)
        if self.error:
            raise self.error
        body = self.responses.pop(0) if self.responses else {}
        return io.BytesIO(json.dumps(body).encode())


def test_lidarr_rescan_never_adds_artists():
    recorder = Recorder([{"appName": "Lidarr", "version": "2.9.6.4552"}, {"id": 12}])
    client = LidarrClient("http://nas:8686", KEY, opener=recorder)
    assert client.version() == "2.9.6.4552"
    client.rescan(["/music/B", "/music/A", "/music/A"])
    status, command = recorder.requests
    assert urlsplit(status.full_url).path == "/api/v1/system/status" and status.get_method() == "GET"
    assert command.get_method() == "POST" and urlsplit(command.full_url).path == "/api/v1/command"
    assert command.get_header("X-api-key") == KEY and KEY not in command.full_url
    assert json.loads(command.data) == {"name": "RescanFolders", "folders": ["/music/A", "/music/B"],
                                        "addNewArtists": False}


def test_lidarr_errors_are_explained():
    client = LidarrClient("http://nas:8686", KEY, opener=Recorder(error=HTTPError("u", 401, "x", {}, None)))
    with pytest.raises(LidarrUnavailable, match="clé API"):
        client.version()
    with pytest.raises(ValueError):
        LidarrClient("ftp://nas", KEY)
    with pytest.raises(ValueError, match="Clé API"):
        LidarrClient("http://nas:8686", "clé€")


def test_jellyfin_is_told_which_folders_changed():
    recorder = Recorder()
    JellyfinClient("http://nas:8096", "jellykey", opener=recorder).report_updated(["/media/Musique/A"])
    [request] = recorder.requests
    assert request.get_method() == "POST" and urlsplit(request.full_url).path == "/Library/Media/Updated"
    assert json.loads(request.data) == {"Updates": [{"Path": "/media/Musique/A", "UpdateType": "Modified"}]}
    assert 'Token="jellykey"' in request.get_header("Authorization")


@pytest.mark.parametrize("local,server", [
    ("/mnt/nas/Musique/Artiste/Album", "/music/Artiste/Album"),
    ("/mnt/nas/Musique", "/music"),
    ("/mnt/nas/MusiqueX/Album", None),            # limite de dossier respectée
    ("/ailleurs/Album", None),
])
def test_local_paths_are_translated_for_the_service(local, server):
    assert to_server(local, [PathMapping("/music", "/mnt/nas/Musique")]) == server


def test_windows_service_paths_and_no_mapping():
    assert to_server("/mnt/nas/Musique/A/B", [PathMapping("D:\\Musique", "/mnt/nas/Musique")]) == "D:\\Musique\\A\\B"
    assert to_server("/srv/A", []) == "/srv/A"  # même machine : chemin identique


def test_a_failing_service_never_blocks_the_other():
    class Broken:
        def rescan(self, folders):
            raise RuntimeError("hors ligne")

    class Fine:
        def __init__(self):
            self.paths = None

        def report_updated(self, paths):
            self.paths = paths

    fine = Fine()
    messages = ServiceNotifier(Broken(), [], fine, [PathMapping("/media", "/mnt")]).notify(["/mnt/A", "/x/B"])
    assert messages[0] == "Lidarr non prévenu : hors ligne"
    assert messages[1] == "Jellyfin prévenu (1 dossier(s)), 1 sans correspondance de chemin"
    assert fine.paths == ["/media/A"]


def test_window_notifies_after_a_tag_write_and_never_stores_keys(tmp_path):
    from PySide6.QtWidgets import QApplication
    from musicscope.advanced_gui import AdvancedWindow
    from musicscope.demo import create_demo
    app = QApplication.instance() or QApplication([])
    root = create_demo(tmp_path / "musique")
    window = AdvancedWindow(tmp_path / "cache")
    calls = {}

    class FakeLidarr:
        def __init__(self, server, key):
            calls["lidarr_key"] = key

        def rescan(self, folders):
            calls["lidarr"] = folders

    class FakeJellyfin:
        def __init__(self, server, key):
            pass

        def report_updated(self, paths):
            calls["jellyfin"] = paths

    try:
        window.root = str(root)
        window._lidarr_factory, window._jellyfin_factory = FakeLidarr, FakeJellyfin
        window.configure_lidarr({"server": "http://nas:8686", "api_key": KEY,
                                 "mappings": [("/music", str(root))], "notify": True})
        window._jellyfin_config = {"server": "http://nas:8096", "key": "jk", "mappings": [("/media/Musique", str(root))]}
        assert KEY not in (window.cache_dir / "preferences.json").read_text("utf-8")
        window.start_scan()
        settle(window, app)
        row = next(index for index, album in enumerate(window.inventory.albums) if album.path.endswith("Horizons MP3"))
        window.album_table.selectRow(window.album_proxy.mapFromSource(window.album_model.index(row, 0)).row())
        app.processEvents()
        window.edit_tags(dialog_values=({"genre": "Jazz"}, {}), confirm=True)
        settle(window, app)
        deadline = time.monotonic() + 5
        while "jellyfin" not in calls and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert calls["lidarr"] == ["/music/Artiste Démo/Horizons MP3"]
        assert calls["jellyfin"] == ["/media/Musique/Artiste Démo/Horizons MP3"]
        assert "Lidarr prévenu" in window.log_details.toPlainText()
        window.configure_lidarr({"server": "http://nas:8686", "api_key": KEY, "mappings": [], "notify": False})
        assert window.notify_changed({str(root)}) is None  # notification désactivée
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
