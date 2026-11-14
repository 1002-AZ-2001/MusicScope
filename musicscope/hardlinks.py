"""Diagnostic des liens physiques (Lidarr, qBittorrent) sous une racine.

Deux chemins portant le même périphérique et le même inode désignent le même
fichier : le retirer d'un endroit ne libère aucun espace. Le nombre de liens
(`st_nlink`) indique qu'un fichier existe aussi ailleurs, même hors de la racine.
La fiabilité de ces informations dépend du montage : local et NFS les
transmettent ; SMB/CIFS seulement avec les numéros d'inode du serveur.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import stat as stat_module
from threading import Event

from .cache import check_cancel
from .identity import LOCAL_POSIX_FILESYSTEMS, mount_details

NFS = {"nfs", "nfs4"}
SMB = {"cifs", "smb3", "smbfs"}
RELIABLE, LIKELY, UNRELIABLE, UNKNOWN = "Fiable", "Probable", "Non fiable", "Inconnue"


@dataclass
class LinkDiagnostic:
    root: str
    filesystem: str = "inconnu"
    mount_point: str = ""
    options: list[str] = field(default_factory=list)
    examined: int = 0
    linked_files: int = 0
    shared_pairs: int = 0
    examples: list[tuple[str, int]] = field(default_factory=list)
    reliability: str = UNKNOWN
    reasons: list[str] = field(default_factory=list)


def diagnose_links(root: str | Path, limit: int = 20000, cancel: Event | None = None, progress=None) -> LinkDiagnostic:
    root = Path(root).absolute()
    result = LinkDiagnostic(str(root))
    details = mount_details(root) if os.name == "posix" else None
    if details is not None:
        result.filesystem, result.mount_point = details.filesystem, details.mount_point
        result.options = sorted(option for option in details.options
                                if option.split("=")[0] in {"serverino", "noserverino", "unix", "nounix", "posix", "vers"})
    seen: dict[tuple[int, int], int] = {}
    pending = [root]
    while pending and result.examined < limit:
        check_cancel(cancel)
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat_module.S_ISDIR(info.st_mode):
                if not entry.name.startswith("."):
                    pending.append(entry.path)
                continue
            if not stat_module.S_ISREG(info.st_mode):
                continue
            result.examined += 1
            key = (info.st_dev, info.st_ino)
            if key in seen:
                result.shared_pairs += 1
            seen[key] = seen.get(key, 0) + 1
            if info.st_nlink > 1:
                result.linked_files += 1
                if len(result.examples) < 5:
                    result.examples.append((entry.path, info.st_nlink))
            if result.examined >= limit:
                break
        if progress:
            progress(result.examined, 0, f"Liens physiques : {directory}")
    _conclude(result, details)
    return result


def _conclude(result: LinkDiagnostic, details) -> None:
    fs = result.filesystem
    seen_links = result.linked_files > 0
    if os.name == "nt":
        result.reliability = LIKELY if seen_links else UNKNOWN
        result.reasons.append("Sous Windows, le nombre de liens est fiable sur NTFS local ; sur un partage réseau, "
                              "il dépend du serveur.")
    elif fs in LOCAL_POSIX_FILESYSTEMS:
        result.reliability = RELIABLE
        result.reasons.append("Système de fichiers local : nombre de liens et inodes exacts.")
    elif fs in NFS:
        result.reliability = RELIABLE if seen_links else LIKELY
        result.reasons.append("NFS transmet les inodes et le nombre de liens du serveur.")
    elif fs in SMB:
        options = details.options if details else frozenset()
        if "noserverino" in options:
            result.reliability = UNRELIABLE
            result.reasons.append("Montage SMB avec « noserverino » : les inodes sont inventés par ce poste, "
                                  "les liens ne peuvent pas être détectés.")
        elif seen_links:
            result.reliability = RELIABLE
            result.reasons.append("Montage SMB avec les inodes du serveur, et des fichiers liés ont été vus : "
                                  "le serveur transmet bien les liens.")
        else:
            result.reliability = UNKNOWN
            result.reasons.append("Montage SMB : aucun fichier lié n’a été vu. Soit il n’y en a pas sous cette racine, "
                                  "soit le serveur ne transmet pas le nombre de liens (extensions Unix/POSIX de Samba).")
    else:
        result.reliability = LIKELY if seen_links else UNKNOWN
        result.reasons.append(f"Système de fichiers « {fs} » : fiabilité non vérifiée.")
    if seen_links:
        result.reasons.append(f"{result.linked_files} fichier(s) sur {result.examined} ont au moins un autre lien "
                              "(par exemple le dossier de téléchargement de qBittorrent).")
    if result.shared_pairs:
        result.reasons.append(f"{result.shared_pairs} chemin(s) sous la racine désignent un fichier déjà vu : "
                              "ce ne sont pas des doublons, mais le même fichier.")
    if result.reliability in (UNKNOWN, UNRELIABLE):
        result.reasons.append("Par prudence, vérifiez un fichier importé par Lidarr avec : stat -c '%h %i' FICHIER "
                              "(sur le NAS, le premier nombre vaut 2 pour un fichier lié).")
