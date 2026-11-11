"""Dialogues de l'éditeur de tags : saisie, aperçu des différences avant écriture."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QDialogButtonBox, QFormLayout, QHeaderView, QLabel, QLineEdit,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from .models import Album
from .tagwriter import FIELD_LABELS, FileEdit, describe, plan_album_edits, proposals_to_values

ALBUM_FIELDS = ("albumartist", "album", "date", "genre")
TRACK_FIELDS = ("discnumber", "tracknumber", "title", "artist")
MIXED = "Valeurs différentes selon les pistes — laisser vide pour les conserver"


class TagEditorDialog(QDialog):
    """Saisie des tags d'un album ; rien n'est écrit par ce dialogue."""

    def __init__(self, album: Album, proposals=None, parent=None):
        super().__init__(parent)
        self.album = album
        self.tracks = album.ordered_tracks
        self.setWindowTitle(f"Modifier les tags — {album.value('album') or Path(album.path).name}")
        self.resize(980, 640)
        layout = QVBoxLayout(self)
        intro = QLabel("Les champs communs s’appliquent à toutes les pistes. Séparez plusieurs valeurs par « ; ». "
                       "Un champ vidé est supprimé. Un aperçu est affiché avant toute écriture.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        self.album_inputs: dict[str, QLineEdit] = {}
        self.touched: set[str] = set()
        for key in ALBUM_FIELDS:
            values = {track.value(key) for track in self.tracks}
            line = QLineEdit(next(iter(values)) if len(values) == 1 else "")
            if len(values) > 1:
                line.setPlaceholderText(MIXED)
            line.textEdited.connect(lambda _text, key=key: self.touched.add(key))
            self.album_inputs[key] = line
            form.addRow(FIELD_LABELS[key], line)
        layout.addLayout(form)
        self.table = QTableWidget(len(self.tracks), 1 + len(TRACK_FIELDS))
        self.table.setHorizontalHeaderLabels(["Fichier", *(FIELD_LABELS[key] for key in TRACK_FIELDS)])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        for row, track in enumerate(self.tracks):
            name = QTableWidgetItem(Path(track.path).name)
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name.setToolTip(track.path)
            self.table.setItem(row, 0, name)
            for column, key in enumerate(TRACK_FIELDS, 1):
                self.table.setItem(row, column, QTableWidgetItem(track.value(key)))
        self.table.resizeColumnsToContents()
        for column, minimum in ((0, 170), (1, 70), (2, 60), (3, 260)):
            self.table.setColumnWidth(column, max(self.table.columnWidth(column), minimum))
        layout.addWidget(self.table, 1)
        self.proposals = list(proposals or [])
        self.proposals_applied = False
        self.proposal_button = QPushButton("Reprendre les propositions MusicBrainz")
        self.proposal_button.setEnabled(bool(self.proposals))
        self.proposal_button.setToolTip("Remplit les champs avec l’édition probable vérifiée ; rien n’est écrit avant l’aperçu."
                                        if self.proposals else "Vérifiez d’abord cet album avec MusicBrainz.")
        self.proposal_button.clicked.connect(self.apply_proposals)
        layout.addWidget(self.proposal_button, 0, Qt.AlignmentFlag.AlignLeft)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Annuler")
        self.preview_button = buttons.addButton("Aperçu des modifications…", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def apply_proposals(self):
        self.proposals_applied = True
        album_values, track_values = proposals_to_values(self.proposals, self.album)
        for key, value in album_values.items():
            if key in self.album_inputs:
                self.album_inputs[key].setText(value)
                self.touched.add(key)
        for row, track in enumerate(self.tracks):
            for key, value in track_values.get(track.path, {}).items():
                if key in TRACK_FIELDS:
                    item = self.table.item(row, 1 + TRACK_FIELDS.index(key))
                    item.setText(value)
                    item.setForeground(QColor("#ffd27f"))
                elif key in self.album_inputs:
                    # Proposition différente selon les pistes : appliquée piste par piste.
                    self.album_inputs[key].setPlaceholderText(MIXED)

    def album_values(self) -> dict[str, str]:
        values = {}
        for key, line in self.album_inputs.items():
            mixed = line.placeholderText() == MIXED
            if mixed and key not in self.touched and not line.text():
                continue
            values[key] = line.text()
        return values

    def track_values(self) -> dict[str, dict[str, str]]:
        _, mb_tracks = (proposals_to_values(self.proposals, self.album) if self.proposals_applied else ({}, {}))
        result = {}
        for row, track in enumerate(self.tracks):
            values = {key: self.table.item(row, column).text() for column, key in enumerate(TRACK_FIELDS, 1)}
            # Champs d'album proposés piste par piste (valeurs différentes) : conservés.
            for key, value in mb_tracks.get(track.path, {}).items():
                if key in ALBUM_FIELDS and key not in self.touched:
                    values[key] = value
            result[track.path] = values
        return result

    def edits(self) -> list[FileEdit]:
        return plan_album_edits(self.album, self.album_values(), self.track_values())


class TagPreviewDialog(QDialog):
    """Différences avant/après ; l'écriture demande encore ce bouton explicite."""

    def __init__(self, edits: list[FileEdit], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Aperçu des modifications de tags")
        self.resize(900, 520)
        layout = QVBoxLayout(self)
        rows = describe(edits)
        summary = QLabel(f"{len(rows)} modification(s) sur {len(edits)} fichier(s). Les valeurs d’origine sont "
                         "enregistrées dans le journal avant l’écriture ; la modification pourra être annulée "
                         "depuis la page Journal des tags.")
        summary.setWordWrap(True)
        layout.addWidget(summary)
        self.table = QTableWidget(len(rows), 4)
        self.table.setHorizontalHeaderLabels(["Fichier", "Champ", "Avant", "Après"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for row, values in enumerate(rows):
            for column, value in enumerate(values):
                item = QTableWidgetItem(value or "—")
                if column == 3:
                    item.setForeground(QColor("#8fd1a8" if value else "#ff9e80"))
                    if not value:
                        item.setText("(supprimé)")
                self.table.setItem(row, column, item)
        self.table.resizeColumnsToContents()
        for column, minimum in ((0, 170), (1, 120), (2, 220)):
            self.table.setColumnWidth(column, max(self.table.columnWidth(column), minimum))
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Annuler")
        self.write_button = buttons.addButton(f"Écrire les tags ({len(edits)} fichier(s))",
                                              QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
