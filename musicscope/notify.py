"""Prévenir Lidarr et Jellyfin des dossiers modifiés (rangement, quarantaine, tags).

Les appels partent dans un thread à part, après l'opération : une erreur de service est
signalée, jamais bloquante, et rien n'est envoyé sans réglage explicite de l'utilisateur.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import PurePosixPath, PureWindowsPath
import re

from PySide6.QtCore import QThread, Signal


def to_server(local: str, mappings) -> str | None:
    """Chemin vu par le service : préfixe local le plus précis remplacé par son préfixe serveur.
    Sans correspondance : chemin identique (service sur la même machine), sinon None."""
    if not mappings:
        return local
    best = None
    for mapping in mappings:
        prefix = mapping.local_prefix.rstrip("/\\")
        if local == prefix or local.startswith(prefix + os.sep):
            if best is None or len(prefix) > len(best[0]):
                best = (prefix, mapping.server_prefix)
    if best is None:
        return None
    rest = local[len(best[0]):].lstrip("/\\")
    windows = bool(re.match(r"^[a-zA-Z]:|^\\\\", best[1]))
    base = (PureWindowsPath if windows else PurePosixPath)(best[1])
    return str(base.joinpath(*[part for part in re.split(r"[/\\]", rest) if part])) if rest else str(base)


@dataclass
class ServiceNotifier:
    lidarr: object | None = None
    lidarr_mappings: list = field(default_factory=list)
    jellyfin: object | None = None
    jellyfin_mappings: list = field(default_factory=list)

    @property
    def enabled(self) -> bool:
        return self.lidarr is not None or self.jellyfin is not None

    def notify(self, folders) -> list[str]:
        folders = sorted({str(folder) for folder in folders if folder})
        messages = []
        for name, client, mappings, call in (
                ("Lidarr", self.lidarr, self.lidarr_mappings, "rescan"),
                ("Jellyfin", self.jellyfin, self.jellyfin_mappings, "report_updated")):
            if client is None or not folders:
                continue
            server = [path for path in (to_server(folder, mappings) for folder in folders) if path]
            missing = len(folders) - len(server)
            try:
                getattr(client, call)(server)
                messages.append(f"{name} prévenu ({len(server)} dossier(s))"
                                + (f", {missing} sans correspondance de chemin" if missing else ""))
            except Exception as exc:  # jamais bloquant : l'opération locale est déjà faite
                messages.append(f"{name} non prévenu : {exc}")
        return messages


class NotifyThread(QThread):
    report = Signal(str)

    def __init__(self, notifier: ServiceNotifier, folders, parent=None):
        super().__init__(parent)
        self.notifier, self.folders = notifier, list(folders)

    def run(self):
        self.report.emit(" ; ".join(self.notifier.notify(self.folders)))
