"""Mise à jour automatique (v0.18.0) : contrôle, vérifications, installation, interface."""
import hashlib
import io
import json
import os
import subprocess
import tarfile
import time
from pathlib import Path
from urllib.error import HTTPError

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from musicscope import __version__
from musicscope.updater import (
    API_URL,
    DOWNLOAD_PREFIX,
    PUBLISHER,
    Installation,
    UpdateError,
    asset_name,
    detect_installation,
    download,
    due,
    fetch_latest,
    install,
    install_command,
    is_newer,
    parse_version,
    replace_portable,
)

NEXT = "99.0.0"
TAG = f"v{NEXT}"


def portable_archive(tmp_path, marker=b"nouvelle", extra=None) -> bytes:
    """Archive au format publié : tout sous MusicScope/, exécutable MusicScope."""
    source = tmp_path / "construction" / "MusicScope"
    source.mkdir(parents=True)
    (source / "MusicScope").write_bytes(b"#!/bin/sh\n# " + marker + b"\n")
    (source / "MusicScope").chmod(0o755)
    (source / "_internal").mkdir()
    (source / "_internal" / "lib.so").write_bytes(marker)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        tar.add(source, arcname="MusicScope")
        if extra:
            extra(tar)
    return buffer.getvalue()


class Server:
    """Faux GitHub : API des versions et fichiers, tels que l'API les décrit réellement."""

    def __init__(self, files: dict[str, bytes], uploader=PUBLISHER, digests=None, draft=False, sums=None):
        self.files, self.requests = files, []
        sums = sums if sums is not None else "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n"
                                                    for name, data in files.items())
        self.files["SHA256SUMS"] = sums.encode()
        digests = digests or {}
        self.release = {
            "tag_name": TAG, "draft": draft, "prerelease": False,
            "html_url": f"https://github.com/1002-AZ-2001/MusicScope/releases/tag/{TAG}",
            "published_at": "2026-11-22T10:00:00Z",
            "assets": [{"name": name, "size": len(data), "state": "uploaded", "uploader": {"login": uploader},
                        "digest": digests.get(name, "sha256:" + hashlib.sha256(data).hexdigest()),
                        "browser_download_url": f"{DOWNLOAD_PREFIX}{TAG}/{name}"}
                       for name, data in self.files.items()],
        }

    def __call__(self, request, timeout):
        self.requests.append(request.full_url)
        assert request.get_header("User-agent").startswith("MusicScope/")
        if request.full_url == API_URL:
            return io.BytesIO(json.dumps(self.release).encode())
        name = request.full_url.rsplit("/", 1)[-1]
        return io.BytesIO(self.files[name])


def portable(tmp_path) -> Installation:
    app_dir = tmp_path / "apps" / "MusicScope"
    (app_dir / "_internal").mkdir(parents=True)
    (app_dir / "MusicScope").write_bytes(b"ancienne")
    return Installation("portable", app_dir, "portable")


@pytest.mark.parametrize("text,expected", [("v1.2.3", (1, 2, 3)), ("0.18.0", (0, 18, 0)), ("v1.2", None),
                                           ("v1.2.3-rc1", None), ("", None), (None, None)])
def test_versions(text, expected):
    assert parse_version(text) == expected


def test_newer_compares_numbers_not_text():
    assert is_newer("0.18.0", "0.9.9") and not is_newer("0.17.0", "0.17.0") and not is_newer("0.16.0", "0.17.0")
    assert not is_newer("rien", "0.17.0")


def test_check_interval():
    now = time.time()
    assert due(None, now) and due(now - 25 * 3600, now) and not due(now - 3600, now)
    assert due(now + 3600, now)  # horloge revenue en arrière : on recontrôle


def test_installation_is_detected(tmp_path):
    opt = tmp_path / "opt" / "musicscope"
    opt.mkdir(parents=True)
    dpkg, pacman = tmp_path / "dpkg", tmp_path / "pacman"
    dpkg.mkdir()
    (pacman / "musicscope-0.17.0-1").mkdir(parents=True)
    kwargs = {"frozen": True, "machine": "x86_64", "opt_dir": opt, "dpkg_info": dpkg, "pacman_local": pacman}
    assert detect_installation(opt / "MusicScope", **kwargs).kind == "unknown"   # aucun paquet ne le déclare
    (dpkg / "musicscope.list").write_text(f"/.\n{opt}\n{opt}/MusicScope\n")
    assert detect_installation(opt / "MusicScope", **kwargs).kind == "deb"
    (dpkg / "musicscope.list").unlink()
    (pacman / "musicscope-0.17.0-1" / "files").write_text(f"%FILES%\n{str(opt).lstrip('/')}/MusicScope\n")
    assert detect_installation(opt / "MusicScope", **kwargs).kind == "arch"
    app = tmp_path / "home" / "MusicScope"
    app.mkdir(parents=True)
    assert detect_installation(app / "MusicScope", **kwargs).kind == "portable"
    assert detect_installation(app / "MusicScope", frozen=False).kind == "source"
    assert detect_installation(app / "MusicScope", frozen=True, machine="aarch64").kind == "unknown"


def test_latest_release_is_read(tmp_path):
    server = Server({asset_name("deb", NEXT): b"deb"})
    release = fetch_latest(server)
    assert (release.version, release.tag, release.newer) == (NEXT, TAG, True)
    assert release.assets[asset_name("deb", NEXT)].uploader == PUBLISHER


@pytest.mark.parametrize("error,message", [(HTTPError(API_URL, 403, "x", {}, None), "Limite"),
                                           (HTTPError(API_URL, 404, "x", {}, None), "Aucune version"),
                                           (OSError("réseau coupé"), "injoignable")])
def test_check_errors_are_explained(error, message):
    def opener(request, timeout):
        raise error
    with pytest.raises(UpdateError, match=message):
        fetch_latest(opener)


def test_drafts_and_garbage_are_refused():
    with pytest.raises(UpdateError, match="inattendue"):
        fetch_latest(Server({}, draft=True))
    with pytest.raises(UpdateError, match="illisible"):
        fetch_latest(lambda request, timeout: io.BytesIO(b"<html>"))


def test_package_is_downloaded_and_verified(tmp_path):
    name = asset_name("deb", NEXT)
    server = Server({name: b"paquet" * 1000})
    (tmp_path / "maj").mkdir()
    (tmp_path / "maj" / "ancien.deb").write_bytes(b"x")
    package = download(fetch_latest(server), Installation("deb", None, ""), tmp_path / "maj", server)
    assert package.read_bytes() == b"paquet" * 1000 and package.name == name
    assert sorted(path.name for path in (tmp_path / "maj").iterdir()) == [name]  # ancien téléchargement retiré


@pytest.mark.parametrize("server_kwargs,message", [
    ({"uploader": "quelqu-un"}, "workflow de publication"),
    ({"digests": {asset_name("deb", NEXT): "sha256:" + "0" * 64}}, "diffèrent"),
    ({"sums": "0" * 64 + "  " + asset_name("deb", NEXT) + "\n"}, "SHA-256"),
    ({"sums": "0" * 64 + "  autre.deb\n"}, "ne figure pas"),
])
def test_tampered_releases_are_refused(tmp_path, server_kwargs, message):
    name = asset_name("deb", NEXT)
    server = Server({name: b"paquet"}, **server_kwargs)
    if "sums" in server_kwargs:  # SHA256SUMS et digest cohérents entre eux, fichier différent
        for asset in server.release["assets"]:
            if asset["name"] == name:
                asset["digest"] = "sha256:" + "0" * 64
    with pytest.raises(UpdateError, match=message):
        download(fetch_latest(server), Installation("deb", None, ""), tmp_path / "maj", server)
    assert not list((tmp_path / "maj").glob("*.deb*")) if (tmp_path / "maj").exists() else True


def test_oversized_or_cancelled_download_leaves_nothing(tmp_path):
    import threading

    from musicscope.cache import ScanCancelled
    name = asset_name("deb", NEXT)
    server = Server({name: b"paquet"})
    release = fetch_latest(server)
    server.files[name] = b"paquet plus long que prevu"
    with pytest.raises(UpdateError, match="taille annoncée"):
        download(release, Installation("deb", None, ""), tmp_path / "maj", server)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ScanCancelled):
        download(release, Installation("deb", None, ""), tmp_path / "maj", server, cancel)
    assert list((tmp_path / "maj").iterdir()) == []


def test_packages_are_installed_through_polkit(tmp_path):
    package = tmp_path / "musicscope_99.0.0-1_amd64.deb"
    tools = {"pkexec": "/usr/bin/pkexec", "apt-get": "/usr/bin/apt-get", "pacman": "/usr/bin/pacman"}
    which = tools.get
    assert install_command(Installation("deb", None, ""), package, which) == [
        "/usr/bin/pkexec", "/usr/bin/apt-get", "install", "-y", "--no-install-recommends", str(package)]
    assert install_command(Installation("arch", None, ""), package, which) == [
        "/usr/bin/pkexec", "/usr/bin/pacman", "-U", "--noconfirm", str(package)]
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")
    result = install(Installation("deb", None, ""), package, runner, which)
    assert calls and result.restart and "installée" in result.message
    with pytest.raises(UpdateError, match="annulée"):
        install(Installation("deb", None, ""), package,
                lambda command, **kwargs: subprocess.CompletedProcess(command, 126, "", ""), which)
    with pytest.raises(UpdateError, match="sudo apt install"):
        install_command(Installation("deb", None, ""), package, lambda name: None)


def test_portable_copy_is_swapped_and_previous_kept(tmp_path):
    installation = portable(tmp_path)
    archive = tmp_path / "maj.tar.gz"
    archive.write_bytes(portable_archive(tmp_path))
    result = install(installation, archive)
    app_dir = installation.app_dir
    assert b"nouvelle" in (app_dir / "MusicScope").read_bytes() and os.access(app_dir / "MusicScope", os.X_OK)
    assert (app_dir.parent / "MusicScope.precedent" / "MusicScope").read_bytes() == b"ancienne"
    assert result.restart == [str(app_dir / "MusicScope")]
    assert [path.name for path in app_dir.parent.iterdir() if path.name.startswith(".musicscope-maj")] == []
    archive.write_bytes(portable_archive(tmp_path / "2", b"encore"))
    install(installation, archive)  # deuxième mise à jour : seule la version précédente est gardée
    assert b"nouvelle" in (app_dir.parent / "MusicScope.precedent" / "MusicScope").read_bytes()


def hostile(tar, name, link=None):
    info = tarfile.TarInfo(name)
    if link:
        info.type, info.linkname = tarfile.SYMTYPE, link
        tar.addfile(info)
    else:
        info.size = 4
        tar.addfile(info, io.BytesIO(b"mal!"))


@pytest.mark.parametrize("extra,message", [
    (lambda tar: hostile(tar, "../evade"), "inattendue"),
    (lambda tar: hostile(tar, "/etc/evade"), "inattendue"),
    (lambda tar: hostile(tar, "MusicScope/lien", "/etc/passwd"), "illisible"),
])
def test_hostile_archives_change_nothing(tmp_path, extra, message):
    installation = portable(tmp_path)
    archive = tmp_path / "maj.tar.gz"
    archive.write_bytes(portable_archive(tmp_path, extra=extra))
    with pytest.raises(UpdateError, match=message):
        replace_portable(installation.app_dir, archive)
    assert (installation.app_dir / "MusicScope").read_bytes() == b"ancienne"
    assert not (tmp_path / "evade").exists() and not (tmp_path / "apps" / "MusicScope.precedent").exists()


def test_archive_without_program_is_refused(tmp_path):
    installation = portable(tmp_path)
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        hostile(tar, "MusicScope/LICENSE")
    archive = tmp_path / "maj.tar.gz"
    archive.write_bytes(buffer.getvalue())
    with pytest.raises(UpdateError, match="incomplète"):
        replace_portable(installation.app_dir, archive)
    assert (installation.app_dir / "MusicScope").read_bytes() == b"ancienne"


# Interface
@pytest.fixture
def window(tmp_path):
    from PySide6.QtWidgets import QApplication

    from musicscope.advanced_gui import AdvancedWindow
    app = QApplication.instance() or QApplication([])
    window = AdvancedWindow(tmp_path / "cache")
    yield window, app
    window.close()


def wait(app, condition, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("délai dépassé")


def test_help_menu_offers_update_and_remembers_the_choice(window):
    window, _app = window
    titles = [action.text() for action in window.help_menu.actions()]
    assert titles[:2] == ["Rechercher une mise à jour…", "Rechercher automatiquement chaque jour"]
    window.updater.auto_action.setChecked(False)
    assert window._preferences()["update_check"] is False and not window.updater.check_on_startup()


def test_startup_check_once_a_day_and_skipped_version(window, tmp_path):
    window, app = window
    from musicscope.update_gui import UpdateController
    server = Server({})
    controller = UpdateController(window, fetch=lambda: fetch_latest(server), installation=portable(tmp_path),
                                  ask="skip")
    assert controller.check_on_startup()
    wait(app, lambda: controller.thread is None and "ignorée" in window.state.text())
    assert window._preferences()["update_skipped"] == NEXT
    assert not controller.check_on_startup()          # déjà contrôlé aujourd'hui
    window.save_preferences(update_last_check=None)
    controller.ask = None                             # version ignorée : aucune fenêtre au démarrage
    controller.check_on_startup()
    wait(app, lambda: controller.thread is None and "version ignorée" in window.state.text())


def test_results_are_handled_in_the_interface_thread(window, tmp_path):
    import threading
    window, app = window
    from musicscope.update_gui import UpdateController
    controller = UpdateController(window, fetch=lambda: fetch_latest(Server({})), installation=portable(tmp_path),
                                  ask="later")
    seen = []
    original = controller.offer
    controller.offer = lambda release: seen.append(threading.current_thread()) or original(release)
    controller.check(silent=True)
    wait(app, lambda: seen and controller.thread is None)
    assert seen == [threading.main_thread()]


def test_up_to_date_and_offline_are_quiet_at_startup(window, tmp_path):
    window, app = window
    from musicscope.update_gui import UpdateController
    server = Server({})
    server.release["tag_name"] = f"v{__version__}"
    controller = UpdateController(window, fetch=lambda: fetch_latest(server), installation=portable(tmp_path))
    controller.check(silent=True)
    wait(app, lambda: controller.thread is None and "à jour" in window.state.text())

    def offline():
        raise UpdateError("GitHub injoignable : hors ligne")
    controller.fetch = offline
    controller.check(silent=True)
    wait(app, lambda: controller.thread is None and "hors ligne" in window.state.text())


def test_portable_update_from_the_window_then_restart(window, tmp_path):
    window, app = window
    from musicscope.update_gui import UpdateController
    name = asset_name("portable", NEXT)
    server = Server({name: portable_archive(tmp_path)})
    installation = portable(tmp_path)
    launched = []
    controller = UpdateController(window, installation=installation, opener=server, ask="install",
                                  launcher=lambda program, arguments: launched.append(program) or True)
    controller.fetch = lambda: fetch_latest(server)
    controller.check(silent=False)
    wait(app, lambda: launched)
    assert launched == [str(installation.app_dir / "MusicScope")]
    assert b"nouvelle" in (installation.app_dir / "MusicScope").read_bytes()
    assert not list((window.cache_dir / "updates").iterdir())  # paquet retiré après installation


def test_source_checkout_only_points_to_the_release(window):
    window, _app = window
    from musicscope.update_gui import UpdateController
    controller = UpdateController(window, fetch=lambda: fetch_latest(Server({})),
                                  installation=Installation("source", None, "sources"), ask="install")
    with pytest.raises(UpdateError, match="sources"):
        download(fetch_latest(Server({})), controller.installation, Path(window.cache_dir) / "u", Server({}))


def test_nas_export_from_an_older_version_is_flagged(tmp_path, window):
    import gzip
    window, app = window
    from musicscope.demo import create_demo
    from musicscope.nas import EXPORT_NAME, run_work
    root = create_demo(tmp_path / "Musique")
    run_work(root, tmp_path / "out", tmp_path / "nas-cache", spectrum="none", log=lambda _: None)
    path = tmp_path / "out" / EXPORT_NAME
    data = json.loads(gzip.decompress(path.read_bytes()))
    data["app_version"] = "0.17.0"
    path.write_bytes(gzip.compress(json.dumps(data).encode()))
    window.root = str(root)
    window.import_nas_work(str(path))
    wait(app, lambda: any("Travail du NAS" in line for line in getattr(window, "service_log", [])), 60)
    assert "mettez à jour l’image du NAS" in window.service_log[-1]
    wait(app, lambda: window.task is None and window._after_task is None, 60)
