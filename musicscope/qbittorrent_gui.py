"""Connexion à qBittorrent : quels fichiers de la médiathèque sont partagés.

Les secrets (mot de passe, clé API) restent en mémoire pour la session, sauf si l'utilisateur
les mémorise dans le trousseau du système ; les préférences ne gardent que l'adresse,
l'identifiant et les correspondances de chemins.
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)


class QBittorrentDialog(QDialog):
    def __init__(self, root, previous=None, parent=None):
        super().__init__(parent)
        previous = previous or {}
        self.setWindowTitle("qBittorrent — fichiers partagés (lecture seule)")
        self.resize(820, 500)
        layout = QVBoxLayout(self)
        intro = QLabel("MusicScope lit la liste des torrents et de leurs fichiers, sans rien modifier dans "
                       "qBittorrent. Les fichiers d’un torrent (partagé ou en pause) ne seront ni déplacés, ni mis en "
                       "quarantaine, ni réécrits par l’éditeur de tags.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.server = QLineEdit(previous.get("server", ""))
        self.server.setPlaceholderText("http://truenas:8080 (adresse de la WebUI)")
        self.api_key = QLineEdit(previous.get("api_key", ""))
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("qbt_… (qBittorrent 5.2 et plus : Préférences → WebUI → API Key)")
        self.username = QLineEdit(previous.get("username", ""))
        self.password = QLineEdit(previous.get("password", ""))
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Adresse de la WebUI", self.server)
        form.addRow("Clé API", self.api_key)
        form.addRow("ou identifiant", self.username)
        form.addRow("et mot de passe", self.password)
        layout.addLayout(form)
        layout.addWidget(QLabel("Correspondances : dossier vu par qBittorrent (souvent /downloads dans un conteneur) "
                                "→ même dossier sur ce poste. Laissez vide si les chemins sont identiques."))
        self.mapping_table = QTableWidget(0, 2)
        self.mapping_table.setHorizontalHeaderLabels(["Préfixe qBittorrent", "Préfixe local (ce poste)"])
        self.mapping_table.horizontalHeader().setStretchLastSection(True)
        self.mapping_table.setColumnWidth(0, 360)
        for server, local in previous.get("mappings") or [("", "")]:
            self.add_mapping(server, local)
        layout.addWidget(self.mapping_table)
        add_button = QPushButton("Ajouter une correspondance")
        add_button.clicked.connect(lambda: self.add_mapping("", ""))
        layout.addWidget(add_button)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Lire les torrents")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Annuler")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

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
        return {"server": self.server.text().strip(), "api_key": self.api_key.text().strip(),
                "username": self.username.text().strip(), "password": self.password.text(), "mappings": rows}


def secret_names(server: str, username: str) -> tuple[str, str]:
    from .keychain import account
    return account("qbittorrent-cle", server), account("qbittorrent", server, username or "-")


def connect_qbittorrent(window, config=None, client_factory=None, dialog_hook=None):
    """Lit les torrents en tâche de fond et garde l'index pour la session."""
    if window.task is not None:
        return
    from .jellyfin import PathMapping
    from .keychain import recall, remember
    from .qbittorrent import QBittorrentClient, QBittorrentUnavailable
    if config is None:
        from .keychain_gui import RememberRow, attach
        previous = dict(getattr(window, "_qbittorrent_config", None) or window._preferences().get("qbittorrent") or {})
        if previous.get("server") and not (previous.get("api_key") or previous.get("password")):
            key_name, password_name = secret_names(previous["server"], previous.get("username", ""))
            found = recall(window, "qbittorrent", [key_name, password_name])
            previous.update(api_key=found.get(key_name, ""), password=found.get(password_name, ""))
        dialog = QBittorrentDialog(window.root, previous, window)

        def cleared():
            dialog.api_key.clear()
            dialog.password.clear()
        row = RememberRow(window, "qbittorrent",
                          lambda: list(secret_names(dialog.server.text(), dialog.username.text())), cleared)
        attach(dialog, row)
        try:
            accepted = dialog_hook(dialog) if dialog_hook else dialog.exec() == QDialog.DialogCode.Accepted
            if not accepted:
                return
            config = dialog.configuration()
            config["remember"] = row.checked
        finally:
            dialog.deleteLater()
    window._qbittorrent_config = config
    if "remember" in config:
        key_name, password_name = secret_names(config["server"], config.get("username", ""))
        message = remember(window, "qbittorrent", {key_name: config.get("api_key", ""),
                                                   password_name: config.get("password", "")}, bool(config["remember"]))
        if message:
            window.state.setText(message)
    window.save_preferences(qbittorrent={"server": config["server"], "username": config.get("username", ""),
                                         "mappings": config.get("mappings", [])})
    factory = client_factory or QBittorrentClient
    try:
        client = factory(config["server"], config.get("username", ""), config.get("password", ""),
                         config.get("api_key", ""))
    except ValueError as exc:
        window.show_error(f"qBittorrent : {exc}")
        return
    mappings = [PathMapping(*row) for row in config.get("mappings", [])]

    def operation(cancel, progress):
        try:
            return client.seed_index(mappings, cancel, progress)
        except QBittorrentUnavailable as exc:
            return exc

    def done(result):
        if isinstance(result, Exception):
            window.show_error(f"qBittorrent : {result}")
            return
        window.seed_index = result
        window.file_duplicates_page.invalidate("Torrents relus : relancez la recherche des fichiers en double.")
        if hasattr(window, "refresh_duplicates"):
            window.refresh_duplicates()
        window.state.setText(result.summary() + ". Ces fichiers sont désormais protégés.")

    window.run_task("Lecture des torrents qBittorrent…", operation, done)
