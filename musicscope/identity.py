"""Identité physique uniquement lorsque le volume local fournit des identifiants.

Les partages réseau restent indéterminés : des numéros d'inodes synthétiques
peuvent y être réutilisés ou changer d'une connexion à l'autre.
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from typing import Callable


LOCAL_POSIX_FILESYSTEMS = {
    "ext2", "ext3", "ext4", "xfs", "btrfs", "zfs", "tmpfs", "ramfs",
    "overlay", "aufs", "ufs", "f2fs", "jfs", "reiserfs", "bcachefs", "ntfs3",
}
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
FILE_SUPPORTS_HARD_LINKS = 0x00400000


@dataclass(frozen=True)
class WindowsVolume:
    drive_type: int
    filesystem: str
    serial: int
    flags: int


def _windows_volume(path: str) -> WindowsVolume | None:
    """Appels Win32 de consultation ; aucune bibliothèque Windows hors Windows."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_volume_path = kernel32.GetVolumePathNameW
    get_volume_path.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    get_volume_path.restype = wintypes.BOOL
    get_drive_type = kernel32.GetDriveTypeW
    get_drive_type.argtypes = [wintypes.LPCWSTR]
    get_drive_type.restype = wintypes.UINT
    get_volume_info = kernel32.GetVolumeInformationW
    get_volume_info.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD),
                               ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]
    get_volume_info.restype = wintypes.BOOL
    root = ctypes.create_unicode_buffer(32768)
    if not get_volume_path(path, root, len(root)):
        return None
    drive_type = get_drive_type(root.value)
    # Rejeter les lecteurs réseau mappés avant toute autre requête de volume.
    if drive_type not in {DRIVE_FIXED, DRIVE_REMOVABLE}:
        return None
    serial, max_component, flags = wintypes.DWORD(), wintypes.DWORD(), wintypes.DWORD()
    filesystem = ctypes.create_unicode_buffer(64)
    if not get_volume_info(root.value, None, 0, ctypes.byref(serial), ctypes.byref(max_component),
                           ctypes.byref(flags), filesystem, len(filesystem)):
        return None
    return WindowsVolume(drive_type, filesystem.value, serial.value, flags.value)


def windows_file_identity(path: str | Path, info: os.stat_result,
                          volume_probe: Callable[[str], WindowsVolume | None] | None = None
                          ) -> tuple[int | None, int | None]:
    r"""Valide les identifiants natifs st_dev/st_ino sur NTFS/ReFS local.

    Le point d'injection sert aux tests sans prétendre exécuter Win32 sous Linux.
    Les préfixes UNC sont écartés, y compris les chemins étendus \\?\UNC.
    """
    path_text = str(path).replace("/", "\\")
    if path_text.startswith("\\\\") and not re.match(r"^\\\\\?\\[a-zA-Z]:\\", path_text):
        return None, None
    if not info.st_dev or not info.st_ino or info.st_ino < 0:
        return None, None
    try:
        volume = (volume_probe or _windows_volume)(path_text)
    except (OSError, AttributeError, ValueError):
        return None, None
    if (volume is None or volume.drive_type not in {DRIVE_FIXED, DRIVE_REMOVABLE}
            or volume.filesystem.casefold() not in {"ntfs", "refs"}
            or not volume.flags & FILE_SUPPORTS_HARD_LINKS
            or not volume.serial or volume.serial != info.st_dev):
        return None, None
    return info.st_dev, info.st_ino


def file_identity(path: Path, info: os.stat_result) -> tuple[int | None, int | None]:
    if os.name == "nt":
        return windows_file_identity(path, info)
    if os.name != "posix" or not info.st_ino or not info.st_dev:
        return None, None
    try:
        mounts = []
        with open("/proc/self/mountinfo", encoding="utf-8") as stream:
            for line in stream:
                left, right = line.split(" - ", 1)
                mount = re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), left.split()[4])
                if path == Path(mount) or Path(mount) in path.parents:
                    mounts.append((len(mount), right.split()[0]))
        longest = max(length for length, _ in mounts) if mounts else 0
        filesystems = {filesystem for length, filesystem in mounts if length == longest}
        if len(filesystems) != 1 or not filesystems <= LOCAL_POSIX_FILESYSTEMS:
            return None, None
    except (OSError, ValueError, IndexError):
        return None, None
    return info.st_dev, info.st_ino
