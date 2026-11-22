"""Clés dans le trousseau du système (v0.19.0) : seulement sur demande, jamais dans les préférences."""
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from musicscope.keychain import (
    SERVICE,
    Keychain,
    _system_backend,
    account,
    recall,
    remember,
)

KEY = "0123456789abcdef0123456789abcdef"


class FakeBackend:
    def __init__(self, fail=False):
        self.items, self.fail = {}, fail

    def get_password(self, service, name):
        if self.fail:
            raise RuntimeError("trousseau verrouillé")
        return self.items.get((service, name))

    def set_password(self, service, name, value):
        if self.fail:
            raise RuntimeError("déverrouillage refusé")
        self.items[(service, name)] = value

    def delete_password(self, service, name):
        if (service, name) not in self.items:
            raise KeyError(name)
        del self.items[(service, name)]


def test_entry_names():
    assert account("lidarr", " http://NAS:8686/ ") == "lidarr:http://nas:8686"
    assert account("qbittorrent", "http://nas:8080", "admin") == "qbittorrent:http://nas:8080:admin"
    assert account("acoustid") == "acoustid"


def test_keychain_never_raises():
    backend = FakeBackend()
    keychain = Keychain(backend)
    assert keychain.set("a", "secret") and keychain.get("a") == "secret" and backend.items == {(SERVICE, "a"): "secret"}
    assert keychain.delete("a") and not keychain.delete("a") and keychain.get("a") == ""
    broken = Keychain(FakeBackend(fail=True))
    assert broken.get("a") == "" and not broken.set("a", "x") and "refusé" in broken.reason
    absent = Keychain(None)
    assert not absent.available and absent.get("a") == "" and not absent.set("a", "x")


def test_keyring_can_be_disabled(monkeypatch):
    monkeypatch.setenv("MUSICSCOPE_KEYRING", "none")
    backend, reason = _system_backend()
    assert backend is None and "désactivé" in reason


def test_unavailable_system_keyring_is_explained(monkeypatch):
    monkeypatch.delenv("MUSICSCOPE_KEYRING", raising=False)
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/nonexistent/bus")
    backend, reason = _system_backend()
    assert backend is None and "indisponible" in reason


@pytest.fixture
def window(tmp_path):
    from PySide6.QtWidgets import QApplication

    from musicscope.advanced_gui import AdvancedWindow
    app = QApplication.instance() or QApplication([])
    backend = FakeBackend()
    window = AdvancedWindow(tmp_path / "cache")
    window.keychain = Keychain(backend)
    yield window, backend, app
    window.close()


def reopen(tmp_path, backend):
    from musicscope.advanced_gui import AdvancedWindow
    keychain = Keychain(backend)
    AdvancedWindow.keychain = keychain  # le trousseau existe avant la fenêtre (lu à son ouverture)
    try:
        window = AdvancedWindow(tmp_path / "cache")
    finally:
        del AdvancedWindow.keychain
    window.keychain = keychain
    return window


def test_lidarr_key_is_kept_only_when_asked_and_restored_at_startup(window, tmp_path):
    window, backend, _app = window
    from musicscope.services_gui import build_notifier, configure_lidarr
    window.root = str(tmp_path)

    def fill(remember_it):
        def hook(dialog):
            from musicscope.keychain_gui import RememberRow
            row = dialog.findChild(RememberRow)
            assert not row.checkbox.isChecked() or remember_it is None  # décochée par défaut
            dialog.server.setText("http://nas:8686")
            dialog.api_key.setText(KEY)
            if remember_it is not None:
                row.checkbox.setChecked(remember_it)
            return True
        return hook

    configure_lidarr(window, dialog_hook=fill(False))
    assert backend.items == {} and window._lidarr_config["api_key"] == KEY
    configure_lidarr(window, dialog_hook=fill(True))
    assert backend.items == {(SERVICE, "lidarr:http://nas:8686"): KEY}
    assert KEY not in (window.cache_dir / "preferences.json").read_text("utf-8")
    assert "mémorisée" in window.state.text()
    window.close()

    later = reopen(tmp_path, backend)
    try:
        assert later._lidarr_config["api_key"] == KEY and "trousseau" in later.state.text()
        later._lidarr_factory = lambda server, key: ("client", server, key)
        assert build_notifier(later, later._lidarr_factory).lidarr == ("client", "http://nas:8686", KEY)

        def forget(dialog):
            from musicscope.keychain_gui import RememberRow
            row = dialog.findChild(RememberRow)
            assert row.checkbox.isChecked() and dialog.api_key.text() == KEY  # prérempli depuis le trousseau
            row.forget_button.click()
            assert dialog.api_key.text() == "" and not row.checkbox.isChecked()
            return False
        configure_lidarr(later, dialog_hook=forget)
        assert backend.items == {}
    finally:
        later.close()
    again = reopen(tmp_path, backend)
    try:
        assert getattr(again, "_lidarr_config", None) in (None, {})
    finally:
        again.close()


def test_without_a_keychain_the_box_is_disabled_and_nothing_is_stored(window, tmp_path):
    window, backend, _app = window
    from musicscope.keychain_gui import RememberRow
    from musicscope.services_gui import configure_lidarr
    window.keychain = Keychain(None)
    seen = {}

    def hook(dialog):
        row = dialog.findChild(RememberRow)
        seen["enabled"], seen["text"] = row.checkbox.isEnabled(), row.checkbox.text()
        row.checkbox.setChecked(True)
        dialog.server.setText("http://nas:8686")
        dialog.api_key.setText(KEY)
        return True
    configure_lidarr(window, dialog_hook=hook)
    assert seen["enabled"] is False and "indisponible" in seen["text"]
    assert window._lidarr_config["api_key"] == KEY and backend.items == {}
    assert KEY not in (window.cache_dir / "preferences.json").read_text("utf-8")


def test_qbittorrent_password_and_key_are_remembered_per_server_and_user(window, tmp_path):
    window, backend, app = window
    from musicscope.keychain_gui import RememberRow
    from musicscope.qbittorrent import SeedIndex
    from musicscope.qbittorrent_gui import connect_qbittorrent

    class Client:
        def __init__(self, server, username, password, api_key):
            self.args = (server, username, password, api_key)

        def seed_index(self, mappings, cancel=None, progress=None):
            return SeedIndex()

    def hook(dialog):
        dialog.server.setText("http://nas:8080")
        dialog.username.setText("admin")
        dialog.password.setText("mot de passe")
        dialog.findChild(RememberRow).checkbox.setChecked(True)
        return True
    connect_qbittorrent(window, client_factory=Client, dialog_hook=hook)
    settle(window, app)
    assert backend.items == {(SERVICE, "qbittorrent:http://nas:8080:admin"): "mot de passe"}
    assert "mot de passe" not in (window.cache_dir / "preferences.json").read_text("utf-8")
    window._qbittorrent_config = None  # comme après un redémarrage
    seen = {}

    def reopen_hook(dialog):
        seen["password"] = dialog.password.text()
        return False
    connect_qbittorrent(window, client_factory=Client, dialog_hook=reopen_hook)
    assert seen["password"] == "mot de passe"


def test_acoustid_key_dialog(window):
    window, backend, _app = window
    from musicscope.keychain_gui import ask_acoustid_key
    assert ask_acoustid_key(window, answer=("cle-acoustid", True)) == ("cle-acoustid", True)
    assert backend.items == {(SERVICE, "acoustid"): "cle-acoustid"}
    assert recall(window, "acoustid", ["acoustid"]) == {"acoustid": "cle-acoustid"}
    assert ask_acoustid_key(window, answer=("cle-acoustid", False)) == ("cle-acoustid", True)
    assert backend.items == {}  # case décochée : clé oubliée


def test_emptied_field_removes_the_old_value(window):
    window, backend, _app = window
    remember(window, "qbittorrent", {"cle": "ancienne", "mdp": "secret"}, True)
    remember(window, "qbittorrent", {"cle": "", "mdp": "secret"}, True)
    assert backend.items == {(SERVICE, "mdp"): "secret"}


def test_jellyfin_key_is_remembered(window, tmp_path, monkeypatch):
    window, backend, app = window
    from musicscope import jellyfin
    from musicscope.keychain_gui import RememberRow

    class Client:
        def __init__(self, server, key, library_id=""):
            pass

        def diagnose(self, *args, **kwargs):
            raise jellyfin.JellyfinUnavailable("serveur de test")
    monkeypatch.setattr(jellyfin, "JellyfinClient", Client)
    window.root = str(tmp_path)

    def hook(dialog):
        dialog.server.setText("http://nas:8096")
        dialog.token.setText("jellykey")
        dialog.findChild(RememberRow).checkbox.setChecked(True)
        return True
    window.start_jellyfin(dialog_hook=hook)
    settle(window, app)
    assert backend.items == {(SERVICE, "jellyfin:http://nas:8096"): "jellykey"}
    assert "jellykey" not in (window.cache_dir / "preferences.json").read_text("utf-8")


def settle(window, app, timeout=30):
    deadline, idle = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        app.processEvents()
        if window.task is None and getattr(window, "_after_task", None) is None:
            idle = idle or time.monotonic()
            if time.monotonic() - idle > 0.2:
                return
        else:
            idle = None
        time.sleep(0.01)
    raise AssertionError("tâche non terminée")
