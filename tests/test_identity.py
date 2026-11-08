import io
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from musicscope.identity import (
    DRIVE_FIXED, FILE_SUPPORTS_HARD_LINKS, WindowsVolume, file_identity,
    windows_file_identity,
)


def stat_identity(device=42, inode=123456789012345678901234567890):
    return SimpleNamespace(st_dev=device, st_ino=inode)


@pytest.mark.parametrize("filesystem", ["NTFS", "ReFS"])
@pytest.mark.parametrize("path", [r"C:\Musique\piste.flac", r"\\?\C:\Musique\piste.flac"])
def test_windows_local_volume_identity_with_mocked_volume_api(filesystem, path):
    stat = stat_identity()
    probe = lambda path: WindowsVolume(DRIVE_FIXED, filesystem, 42, FILE_SUPPORTS_HARD_LINKS)
    assert windows_file_identity(path, stat, probe) == (stat.st_dev, stat.st_ino)


@pytest.mark.parametrize("path", [r"\\nas\musique\piste.flac", r"\\?\UNC\nas\musique\piste.flac"])
def test_windows_unc_is_unknown_without_volume_query(path):
    def forbidden_probe(path):
        pytest.fail("Un chemin UNC ne doit pas interroger son volume")
    assert windows_file_identity(path, stat_identity(), forbidden_probe) == (None, None)


@pytest.mark.parametrize("volume", [
    None,
    WindowsVolume(4, "NTFS", 42, FILE_SUPPORTS_HARD_LINKS),  # lecteur réseau mappé
    WindowsVolume(DRIVE_FIXED, "FAT32", 42, FILE_SUPPORTS_HARD_LINKS),
    WindowsVolume(DRIVE_FIXED, "NTFS", 42, 0),
    WindowsVolume(DRIVE_FIXED, "NTFS", 43, FILE_SUPPORTS_HARD_LINKS),
    WindowsVolume(DRIVE_FIXED, "NTFS", 0, FILE_SUPPORTS_HARD_LINKS),
])
def test_windows_unreliable_or_different_volume_is_unknown(volume):
    assert windows_file_identity(r"Z:\Musique\piste.flac", stat_identity(), lambda _: volume) == (None, None)


def test_windows_native_file_identifier_required():
    probe = lambda _: WindowsVolume(DRIVE_FIXED, "NTFS", 42, FILE_SUPPORTS_HARD_LINKS)
    assert windows_file_identity(r"C:\Musique\piste.flac", stat_identity(inode=0), probe) == (None, None)


def test_windows_volume_probe_error_is_unknown():
    def failed_probe(path):
        raise OSError("Volume API indisponible")
    assert windows_file_identity(r"C:\Musique\piste.flac", stat_identity(), failed_probe) == (None, None)


@pytest.mark.parametrize("filesystem,expected", [("ext4", (42, 8)), ("zfs", (42, 8)),
                                                  ("nfs", (None, None)), ("cifs", (None, None)),
                                                  ("ceph", (None, None)), ("unknownfs", (None, None))])
@pytest.mark.skipif(os.name != "posix", reason="La sonde mountinfo concerne Linux ; Win32 est testé séparément.")
def test_posix_filesystem_identity_is_conservative(filesystem, expected, monkeypatch):
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: io.StringIO(
        f"1 0 1:1 / / rw - ext4 /dev/disk rw\n2 0 2:1 / /music rw - {filesystem} server rw\n"))
    assert file_identity(Path("/music/album/track.flac"), stat_identity(42, 8)) == expected
