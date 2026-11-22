"""Case « Mémoriser dans le trousseau » et bouton « Oublier », communs aux fenêtres de service."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .keychain import account, keychain_for, recall, remember, remembered


class RememberRow(QWidget):
    """`names` donne les entrées du trousseau d'après les champs actuels ; `cleared` vide ces champs."""

    def __init__(self, window, service: str, names, cleared=None, parent=None):
        super().__init__(parent)
        self.window, self.service, self.names, self.cleared = window, service, names, cleared
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.checkbox = QCheckBox("Mémoriser dans le trousseau du système")
        self.forget_button = QPushButton("Oublier la clé enregistrée")
        self.forget_button.clicked.connect(self.forget)
        layout.addWidget(self.checkbox)
        layout.addWidget(self.forget_button)
        layout.addStretch(1)
        stored = remembered(window, service)
        self.checkbox.setChecked(stored)  # décochée par défaut : rien n'est mémorisé sans accord
        self.forget_button.setEnabled(stored)
        keychain = keychain_for(window)
        if not keychain.available:
            self.checkbox.setChecked(False)
            self.checkbox.setEnabled(False)
            self.checkbox.setToolTip(keychain.reason)
            self.checkbox.setText("Mémoriser dans le trousseau (indisponible : clé gardée pour la session)")
        else:
            self.checkbox.setToolTip("Clé chiffrée par le trousseau de votre session (Secret Service). "
                                     "Les préférences de MusicScope n'en contiennent jamais.")

    @property
    def checked(self) -> bool:
        return self.checkbox.isEnabled() and self.checkbox.isChecked()

    def forget(self):
        remember(self.window, self.service, dict.fromkeys(self.names(), ""), False)
        self.checkbox.setChecked(False)
        self.forget_button.setEnabled(False)
        if self.cleared:
            self.cleared()
        self.window.state.setText("Clé retirée du trousseau du système.")


def attach(dialog, row):
    """Place la ligne juste avant les boutons du dialogue (dernier élément de sa mise en page)."""
    layout = dialog.layout() if hasattr(dialog, "layout") else None
    if layout is not None:
        layout.insertWidget(max(layout.count() - 1, 0), row)


def ask_acoustid_key(window, answer=None):
    """Clé AcoustID facultative, préremplie si elle a été mémorisée. Renvoie (clé, validé)."""
    name = account("acoustid")
    previous = recall(window, "acoustid", [name]).get(name, "")
    dialog = QDialog(window)
    dialog.setWindowTitle("AcoustID (facultatif)")
    layout = QVBoxLayout(dialog)
    label = QLabel("Clé API AcoustID — laisser vide pour calculer uniquement l’empreinte locale")
    label.setWordWrap(True)
    layout.addWidget(label)
    field = QLineEdit(previous)
    field.setEchoMode(QLineEdit.EchoMode.Password)
    layout.addWidget(field)
    row = RememberRow(window, "acoustid", lambda: [name], field.clear)
    layout.addWidget(row)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Continuer")
    buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Annuler")
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    try:
        if answer is not None:  # tests : réponse imposée sans fenêtre modale
            field.setText(answer[0])
            row.checkbox.setChecked(answer[1])
            accepted = True
        else:
            accepted = dialog.exec() == QDialog.DialogCode.Accepted
        key = field.text().strip()
        if accepted:
            message = remember(window, "acoustid", {name: key}, row.checked)
            if message:
                window.state.setText(message)
        return key, accepted
    finally:
        dialog.deleteLater()
