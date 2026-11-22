"""Résultat de l'identification MusicBrainz de plusieurs albums ; rien n'est écrit ici."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


def duration_text(seconds: float) -> str:
    minutes = seconds / 60
    return f"{seconds:.0f} s" if seconds < 90 else f"{minutes:.0f} min"


class MusicBrainzBatchDialog(QDialog):
    def __init__(self, results, parent=None):
        super().__init__(parent)
        self.results = results
        writable = sum(result.writable for result in results)
        self.setWindowTitle(f"MusicBrainz : {len(results)} albums examinés")
        self.resize(1000, 560)
        layout = QVBoxLayout(self)
        summary = QLabel(f"{writable} album(s) avec une seule édition cohérente : leurs identifiants MusicBrainz "
                         "peuvent être écrits (titres, dates et noms ne sont pas modifiés). Les autres sont listés "
                         "avec le motif ; traitez-les un par un avec l’éditeur de tags si besoin.")
        summary.setWordWrap(True)
        layout.addWidget(summary)
        self.table = QTableWidget(len(results), 4)
        self.table.setHorizontalHeaderLabels(["Album", "Résultat", "Identifiants à écrire", "Motif ou édition"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for row, result in enumerate(sorted(results, key=lambda item: (not item.writable, item.album.path))):
            name = QTableWidgetItem(Path(result.album.path).name)
            name.setData(Qt.ItemDataRole.UserRole, result.album.path)
            name.setToolTip(result.album.path)
            if result.writable:
                name.setFlags(name.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                name.setCheckState(Qt.CheckState.Checked)
            else:
                name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsUserCheckable)
            detail = result.url or " ".join(result.verification.reasons)
            status = result.verification.status if result.probable or not result.verification.status else \
                f"{result.verification.status} : rien n’est écrit"
            if result.probable and not result.writable:
                status = "Identifiants déjà à jour"
            for column, item in enumerate((name, QTableWidgetItem(status),
                                           QTableWidgetItem(str(result.count) if result.writable else "—"),
                                           QTableWidgetItem(detail))):
                self.table.setItem(row, column, item)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Prévisualiser l’écriture des identifiants")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(writable))
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Fermer")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def chosen(self) -> set[str]:
        paths = set()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item.flags() & Qt.ItemFlag.ItemIsUserCheckable and item.checkState() == Qt.CheckState.Checked:
                paths.add(item.data(Qt.ItemDataRole.UserRole))
        return paths
