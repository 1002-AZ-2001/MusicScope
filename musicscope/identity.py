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


def _unescape_mount(field: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), field)


def read_mount_points() -> dict[str, frozenset[str]]:
    """Points de montage -> types de systèmes de fichiers (montages empilés compris)."""
    table: dict[str, set[str]] = {}
    with open("/proc/self/mountinfo", encoding="utf-8") as stream:
        for line in stream:
            left, right = line.split(" - ", 1)
            mount = str(Path(_unescape_mount(left.split()[4])))
            table.setdefault(mount, set()).add(right.split()[0])
    return {mount: frozenset(types) for mount, types in table.items()}


class MountCache:
    """Instantané des montages pour un parcours, sans relecture à chaque fichier.

    La décision reste celle de la sonde unitaire : le montage le plus profond
    contenant le chemin doit porter un seul type de système de fichiers local.
    Elle est mémorisée par dossier parent et périphérique. Un périphérique non
    encore rencontré déclenche une relecture de la table : un montage ou un
    démontage pendant le scan change le st_dev observé et n'est donc pas
    masqué par l'instantané. Une instance par thread.
    """

    def __init__(self, reader: Callable[[], dict[str, frozenset[str]]] | None = None):
        self._reader = reader or read_mount_points
        self._table: dict[str, frozenset[str]] | None = None
        self._devices: set[int] = set()
        self._decisions: dict[tuple[str, int], bool] = {}
        self._volumes: dict[tuple[str, int], WindowsVolume | None] = {}
        self.reloads = 0

    def _reload(self) -> None:
        self.reloads += 1
        self._decisions.clear()
        self._devices.clear()
        try:
            self._table = self._reader()
        except (OSError, ValueError, IndexError):
            self._table = None

    def _filesystems(self, path: Path) -> frozenset[str]:
        assert self._table is not None
        for candidate in (path, *path.parents):
            types = self._table.get(str(candidate))
            if types is not None:
                return types
        return frozenset()

    def is_local(self, path: Path, device: int) -> bool:
        if device not in self._devices:
            self._reload()
            self._devices.add(device)
        if self._table is None:
            return False
        if str(path) in self._table:
            # Fichier lui-même point de montage (bind mount) : pas de mémoïsation.
            filesystems = self._filesystems(path)
            return len(filesystems) == 1 and filesystems <= LOCAL_POSIX_FILESYSTEMS
        key = (str(path.parent), device)
        decision = self._decisions.get(key)
        if decision is None:
            filesystems = self._filesystems(path.parent)
            decision = len(filesystems) == 1 and filesystems <= LOCAL_POSIX_FILESYSTEMS
            self._decisions[key] = decision
        return decision

    def windows_volume(self, path_text: str, device: int,
                       probe: Callable[[str], WindowsVolume | None]) -> WindowsVolume | None:
        """Volume Win32 mémorisé par dossier parent et numéro de série observé."""
        key = (path_text.rsplit("\\", 1)[0].casefold(), device)
        if key not in self._volumes:
            self._volumes[key] = probe(path_text)
        return self._volumes[key]


def file_identity(path: Path, info: os.stat_result,
                  mounts: MountCache | None = None) -> tuple[int | None, int | None]:
    """Sans `mounts`, la table des montages est relue pour ce seul fichier."""
    if os.name == "nt":
        if mounts is None:
            return windows_file_identity(path, info)
        cache = mounts
        return windows_file_identity(
            path, info, lambda text: cache.windows_volume(text, info.st_dev, _windows_volume))
    if os.name != "posix" or not info.st_ino or not info.st_dev:
        return None, None
    if not (mounts or MountCache()).is_local(Path(path), info.st_dev):
        return None, None
    return info.st_dev, info.st_ino
