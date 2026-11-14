"""Liens physiques (Lidarr, qBittorrent) : jamais traités comme des doublons, diagnostic du montage."""
from __future__ import annotations

import io
import os
import shutil
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from musicscope import hardlinks
from musicscope.cache import Cache
from musicscope.comparison import candidates, compare_albums
from musicscope.duplicates import duplicate_groups
from musicscope.file_duplicates import (
    CONTENT, ScanOptions, collect_files, find_duplicates, select_all_but_best, select_keep_by_date,
)
from musicscope.hardlinks import RELIABLE, UNKNOWN, UNRELIABLE, LIKELY, diagnose_links
from musicscope.identity import MountDetails, mount_details
from musicscope.scanner import scan

from test_tagwriter import SOURCES


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def lidarr(tmp_path):
    """Téléchargement qBittorrent hors racine, bibliothèque Lidarr liée, et une vraie copie en trop."""
    downloads, library = tmp_path / "torrents", tmp_path / "Musique"
    seed = write(downloads / "Album" / "01.flac", b"audio-identique" * 100)
    (library / "NxxxxxS" / "2014 - NxxxxxS - Album").mkdir(parents=True)
    linked = library / "NxxxxxS" / "2014 - NxxxxxS - Album" / "01.flac"
    os.link(seed, linked)
    copy = write(library / "NxxxxxS" / "Album (2014)" / "01.flac", b"audio-identique" * 100)
    return library, linked, copy, seed


def content_groups(root, cache_path):
    options = ScanOptions(frozenset({CONTENT}))
    with Cache(cache_path) as cache:
        return find_duplicates(collect_files(root, options), options, cache=cache)


def test_linked_file_is_kept_and_never_auto_selected(lidarr, tmp_path):
    library, linked, copy, _ = lidarr
    groups = content_groups(library, tmp_path / "c.sqlite3")
    assert len(groups) == 1
    records = {Path(r.path): r for r in groups[0].files}
    assert records[linked].linked and records[linked].nlink == 2 and not records[copy].linked
    assert Path(groups[0].best().path) == linked
    assert select_all_but_best(groups) == {str(copy)}
    assert select_keep_by_date(groups, newest=True) <= {str(copy)}
    assert groups[0].recoverable_bytes == copy.stat().st_size


def test_two_paths_of_the_same_file_are_not_duplicates(tmp_path):
    root = tmp_path / "Musique"
    original = write(root / "A" / "01.flac", b"x" * 500)
    (root / "B").mkdir()
    os.link(original, root / "B" / "01.flac")
    assert content_groups(root, tmp_path / "c.sqlite3") == []


def test_aliases_listed_when_a_real_copy_also_exists(tmp_path):
    root = tmp_path / "Musique"
    original = write(root / "A" / "01.flac", b"x" * 500)
    (root / "B").mkdir()
    os.link(original, root / "B" / "01.flac")
    write(root / "C" / "01.flac", b"x" * 500)
    group = content_groups(root, tmp_path / "c.sqlite3")[0]
    assert len(group.files) == 2 and group.shared_inodes
    merged = next(r for r in group.files if r.aliases)
    assert merged.aliases == [str(root / "B" / "01.flac")]
    assert Path(group.best().path) == root / "A" / "01.flac"
    assert select_all_but_best([group]) == {str(root / "C" / "01.flac")}


def test_diagnostic_on_local_filesystem(lidarr):
    library, linked, _, _ = lidarr
    result = diagnose_links(library)
    assert result.examined == 2 and result.linked_files == 1 and result.shared_pairs == 0
    assert result.examples[0] == (str(linked), 2)
    if result.filesystem in hardlinks.LOCAL_POSIX_FILESYSTEMS:
        assert result.reliability == RELIABLE


@pytest.mark.parametrize("fs,options,links,expected", [
    ("cifs", {"serverino"}, True, RELIABLE),
    ("cifs", {"serverino"}, False, UNKNOWN),
    ("cifs", {"noserverino"}, True, UNRELIABLE),
    ("nfs4", set(), False, LIKELY),
    ("nfs4", set(), True, RELIABLE),
    ("fuse.sshfs", set(), False, UNKNOWN),
])
def test_diagnostic_conclusions_by_mount(tmp_path, monkeypatch, fs, options, links, expected):
    if os.name != "posix":
        pytest.skip("montages Linux")
    root = tmp_path / "Musique"
    original = write(root / "A" / "01.flac", b"x")
    if links:
        os.link(original, tmp_path / "ailleurs.flac")
    monkeypatch.setattr(hardlinks, "mount_details", lambda path: MountDetails("/mnt/nas", fs, frozenset(options)))
    result = diagnose_links(root)
    assert result.reliability == expected
    if expected in (UNKNOWN, UNRELIABLE):
        assert any("stat -c" in reason for reason in result.reasons)


@pytest.mark.skipif(os.name != "posix", reason="mountinfo Linux")
def test_mount_details_reads_super_options(monkeypatch):
    lines = ("1 0 0:1 / / rw - ext4 /dev/sda rw\n"
             "2 1 0:2 / /mnt/TRUENAS\\040NAS rw,relatime - cifs //nas/music rw,vers=3.1.1,serverino,nounix\n")
    monkeypatch.setattr("builtins.open", lambda *a, **k: io.StringIO(lines))
    details = mount_details("/mnt/TRUENAS NAS/Musique")
    assert details.filesystem == "cifs" and details.mount_point == "/mnt/TRUENAS NAS"
    assert {"serverino", "nounix", "vers=3.1.1"} <= details.options
    assert mount_details("/home").filesystem == "ext4"


def tagged_album(folder: Path, fmt="flac"):
    from musicscope.tagwriter import _Tags
    folder.mkdir(parents=True, exist_ok=True)
    for number in (1, 2):
        path = folder / f"{number:02d}.{fmt}"
        shutil.copyfile(str(SOURCES[fmt]), path)
        tags = _Tags(path)
        for key, value in {"album": ["Fujita Scale"], "albumartist": ["NxxxxxS"], "title": [f"P{number}"],
                           "tracknumber": [str(number)]}.items():
            tags.set(key, value)
        tags.save()


def album_groups(root, tmp_path):
    with Cache(tmp_path / "c.sqlite3") as cache:
        inventory = scan(root, cache)
    return duplicate_groups([(a, b, compare_albums(a, b)) for a, b in candidates(inventory.albums)])


def test_album_folders_sharing_files_are_not_proposed(tmp_path):
    root = tmp_path / "Musique"
    tagged_album(root / "NxxxxxS" / "2014 - NxxxxxS - Fujita Scale")
    target = root / "NxxxxxS" / "Fujita Scale (2014)"
    target.mkdir()
    for source in (root / "NxxxxxS" / "2014 - NxxxxxS - Fujita Scale").iterdir():
        os.link(source, target / source.name)
    groups = album_groups(root, tmp_path)
    assert len(groups) == 1 and groups[0].keep is None and groups[0].shared_files
    assert "ne sont pas des copies" in groups[0].reasons[0]


def test_album_linked_to_downloads_is_kept(tmp_path):
    root, downloads = tmp_path / "Musique", tmp_path / "torrents"
    tagged_album(downloads / "Fujita")
    library = root / "NxxxxxS" / "2014 - NxxxxxS - Fujita Scale"
    library.mkdir(parents=True)
    for source in (downloads / "Fujita").iterdir():
        os.link(source, library / source.name)
    tagged_album(root / "NxxxxxS" / "Fujita Scale (2014)")
    groups = album_groups(root, tmp_path)
    assert len(groups) == 1 and not groups[0].shared_files
    group = groups[0]
    assert group.keep is not None and Path(group.albums[group.keep].path) == library
    assert any("liés physiquement" in strength for strength in group.scores[group.keep].strengths)


def test_gui_shows_links_and_runs_diagnostic(lidarr, tmp_path):
    from PySide6.QtCore import QCoreApplication, QEvent
    from PySide6.QtWidgets import QApplication
    from musicscope.advanced_gui import AdvancedWindow
    app = QApplication.instance() or QApplication([])
    library, linked, copy, _ = lidarr
    window = AdvancedWindow(tmp_path / "gui")

    def settle():
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            app.processEvents()
            if window.task is None:
                time.sleep(0.2)
                app.processEvents()
                if window.task is None:
                    return
            time.sleep(0.01)

    try:
        window.root = str(library)
        page = window.file_duplicates_page
        page.min_size.setValue(0)
        page.search()
        settle()
        item = page.items[str(linked)]
        assert item.text(7) == "2" and "Lidarr" in item.toolTip(7)
        assert "1 fichier(s) liés" in page.result_label.text()
        page.apply_selection({str(linked)})
        assert "aucun espace libéré" in page.summary.text()
        window.diagnose_links(show_dialog=False)
        settle()
        assert window.last_link_diagnostic.linked_files == 1
        assert "Détection des liens physiques" in window.state.text()
    finally:
        window.close()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
