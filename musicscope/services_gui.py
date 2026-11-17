"""Réglages de Lidarr et notification des services après une modification."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)


class LidarrDialog(QDialog):
    def __init__(self, root, previous=None, parent=None, notify=True):
        super().__init__(parent)
        previous = previous or {}
        self.setWindowTitle("Lidarr — relecture des dossiers modifiés")
        self.resize(780, 460)
        layout = QVBoxLayout(self)
        intro = QLabel("Après un rangement, une quarantaine ou une écriture de tags, MusicScope peut demander à Lidarr "
                       "de relire les dossiers concernés (commande RescanFolders, sans jamais ajouter d’artiste), et "
                       "signaler ces dossiers à Jellyfin si son diagnostic a été configuré dans cette session.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.server = QLineEdit(previous.get("server", ""))
        self.server.setPlaceholderText("http://truenas:8686")
        self.api_key = QLineEdit(previous.get("api_key", ""))
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("Paramètres → Général → Sécurité → Clé API (session seulement)")
        form.addRow("Adresse de Lidarr", self.server)
        form.addRow("Clé API", self.api_key)
        layout.addLayout(form)
        layout.addWidget(QLabel("Correspondances : dossier vu par Lidarr (ex. /music dans un conteneur) → même dossier "
                                "sur ce poste. Laissez vide si les chemins sont identiques."))
        self.mapping_table = QTableWidget(0, 2)
        self.mapping_table.setHorizontalHeaderLabels(["Préfixe Lidarr", "Préfixe local (ce poste)"])
        self.mapping_table.horizontalHeader().setStretchLastSection(True)
        self.mapping_table.setColumnWidth(0, 340)
        for server, local in previous.get("mappings") or [("", root or "")]:
            self.add_mapping(server, local)
        layout.addWidget(self.mapping_table)
        add_button = QPushButton("Ajouter une correspondance")
        add_button.clicked.connect(lambda: self.add_mapping("", ""))
        layout.addWidget(add_button)
        self.notify = QCheckBox("Prévenir automatiquement Lidarr et Jellyfin après chaque modification")
        self.notify.setChecked(notify)
        layout.addWidget(self.notify)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Enregistrer")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Annuler")
        self.test_button = buttons.addButton("Tester", QDialogButtonBox.ButtonRole.ActionRole)
        self.test_button.clicked.connect(lambda: self.test())
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.client_factory = None

    def add_mapping(self, server, local):
        row = self.mapping_table.rowCount()
        self.mapping_table.insertRow(row)
        self.mapping_table.setItem(row, 0, QTableWidgetItem(server))
        self.mapping_table.setItem(row, 1, QTableWidgetItem(local))

    def configuration(self) -> dict:
        rows = []
        for row in range(self.mapping_table.rowCount()):
            values = tuple(self.mapping_table.item(row, column).text().strip() if self.mapping_table.item(row, column)
                           else "" for column in range(2))
            if all(values):
                rows.append(values)
        return {"server": self.server.text().strip(), "api_key": self.api_key.text().strip(), "mappings": rows,
                "notify": self.notify.isChecked()}

    def test(self) -> bool:
        from .lidarr import LidarrClient, LidarrUnavailable
        config = self.configuration()
        try:
            version = (self.client_factory or LidarrClient)(config["server"], config["api_key"]).version()
        except (ValueError, LidarrUnavailable) as exc:
            self.status.setText(f"Échec : {exc}")
            self.status.setStyleSheet("color:#ff9e80")
            return False
        self.status.setText(f"Connecté à Lidarr {version or '?'}.")
        self.status.setStyleSheet("color:#8fd1a8")
        return True


def configure_lidarr(window, config=None):
    if config is None:
        dialog = LidarrDialog(window.root, getattr(window, "_lidarr_config", None)
                              or window._preferences().get("lidarr") or {}, window,
                              notify=window._preferences().get("notify_services", True))
        try:
            from PySide6.QtWidgets import QDialog as _Dialog
            if dialog.exec() != _Dialog.DialogCode.Accepted:
                return
            config = dialog.configuration()
        finally:
            dialog.deleteLater()
    window._lidarr_config = config  # clé en mémoire seulement
    window.save_preferences(lidarr={"server": config["server"], "mappings": config.get("mappings", [])},
                            notify_services=bool(config.get("notify", True)))
    window.state.setText("Lidarr configuré pour cette session." if config.get("server") else "Lidarr désactivé.")


def build_notifier(window, lidarr_factory=None, jellyfin_factory=None):
    """Services configurés dans la session (clés en mémoire), ou None."""
    from .jellyfin import JellyfinClient, PathMapping
    from .lidarr import LidarrClient
    from .notify import ServiceNotifier
    if not window._preferences().get("notify_services", True):
        return None
    notifier = ServiceNotifier()
    lidarr = getattr(window, "_lidarr_config", None) or {}
    if lidarr.get("server") and lidarr.get("api_key"):
        try:
            notifier.lidarr = (lidarr_factory or LidarrClient)(lidarr["server"], lidarr["api_key"])
            notifier.lidarr_mappings = [PathMapping(*row) for row in lidarr.get("mappings", [])]
        except ValueError:
            notifier.lidarr = None
    jellyfin = getattr(window, "_jellyfin_config", None) or {}
    if jellyfin.get("server") and jellyfin.get("key"):
        try:
            notifier.jellyfin = (jellyfin_factory or JellyfinClient)(jellyfin["server"], jellyfin["key"])
            notifier.jellyfin_mappings = [PathMapping(*row) for row in jellyfin.get("mappings", [])]
        except ValueError:
            notifier.jellyfin = None
    return notifier if notifier.enabled else None


def notify_changed(window, folders):
    """Lance la notification en arrière-plan ; le message s'ajoute à la barre d'état et au journal."""
    from .notify import NotifyThread
    notifier = build_notifier(window, getattr(window, "_lidarr_factory", None), getattr(window, "_jellyfin_factory", None))
    folders = {str(Path(folder)) for folder in folders if folder}
    if notifier is None or not folders:
        return None
    thread = NotifyThread(notifier, folders, window)
    window._notify_threads = [item for item in getattr(window, "_notify_threads", []) if item.isRunning()] + [thread]

    def reported(text):
        if text:
            from datetime import datetime
            line = f"{datetime.now():%H:%M} — {text}"
            window.service_log = (getattr(window, "service_log", []) + [line])[-20:]
            window.log_details.append(line)
            window.state.setText(window.state.text() + " — " + text)
    thread.report.connect(reported)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return thread
