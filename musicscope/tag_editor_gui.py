"""Dialogues de l'éditeur de tags : saisie, aperçu des différences avant écriture."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from .models import Album
from .tagwriter import ARTWORK, FIELD_LABELS, FileEdit, describe, plan_album_edits, proposals_to_values

ALBUM_FIELDS = ("albumartist", "album", "date", "genre")
ALBUM_EXTRA = ("tracktotal", "disctotal", "composer", "conductor", "comment", "musicbrainz_albumid",
               "musicbrainz_albumartistid", "musicbrainz_releasegroupid")
TRACK_FIELDS = ("discnumber", "tracknumber", "title", "artist", "composer", "performer", "musicbrainz_trackid",
                "musicbrainz_releasetrackid")
LIVE_FIELDS = tuple(dict.fromkeys(("tracktotal", "disctotal", "compilation", "composer", "conductor", "performer",
                                   "lyrics", "comment", "musicbrainz_albumid", "musicbrainz_albumartistid",
                                   "musicbrainz_releasegroupid", "musicbrainz_trackid",
                                   "musicbrainz_releasetrackid", ARTWORK)))
MIXED = "Valeurs différentes selon les pistes — laisser vide pour les conserver"
COMPILATION_CHOICES = ("(inchangé)", "Oui", "Non")


def read_live_values(album: Album) -> dict[str, dict[str, str]]:
    """Valeurs actuelles des champs que le scan ne relève pas, lues dans les fichiers de l'album."""
    from .tagwriter import _Tags, join_values
    values = {}
    for track in album.tracks:
        try:
            tags = _Tags(Path(track.path))
        except Exception:
            continue
        current = {}
        for key in LIVE_FIELDS:
            try:
                found = tags.get(key)
            except Exception:
                found = []
            current[key] = found[0] if key in (ARTWORK, "lyrics") and found else join_values(found)
        values[track.path] = current
    return values


class TagEditorDialog(QDialog):
    """Saisie des tags d'un album ; rien n'est écrit par ce dialogue."""

    def __init__(self, album: Album, proposals=None, parent=None, artworks=None, live=None):
        super().__init__(parent)
        self.album = album
        self.tracks = album.ordered_tracks
        self.live = live if live is not None else read_live_values(album)
        self.artworks = [item for group in (artworks or []) for item in group] if artworks else []
        self.setWindowTitle(f"Modifier les tags — {album.value('album') or Path(album.path).name}")
        self.resize(1180, 720)
        layout = QVBoxLayout(self)
        intro = QLabel("Les champs communs s’appliquent à toutes les pistes. Séparez plusieurs valeurs par « ; ». "
                       "Un champ vidé est supprimé. Un aperçu est affiché avant toute écriture. Le chef d’orchestre et "
                       "les interprètes n’existent pas en M4A : ils y sont ignorés.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        top = QHBoxLayout()
        forms = QHBoxLayout()
        self.album_inputs: dict[str, QLineEdit] = {}
        self.touched: set[str] = set()
        for fields in (ALBUM_FIELDS + ("tracktotal", "disctotal"), ALBUM_EXTRA[2:]):
            form = QFormLayout()
            for key in fields:
                values = {self._current(track, key) for track in self.tracks}
                line = QLineEdit(next(iter(values)) if len(values) == 1 else "")
                if len(values) > 1:
                    line.setPlaceholderText(MIXED)
                line.textEdited.connect(lambda _text, key=key: self.touched.add(key))
                self.album_inputs[key] = line
                form.addRow(FIELD_LABELS[key], line)
            if fields[0] == "albumartist":
                self.compilation = QComboBox()
                self.compilation.addItems(COMPILATION_CHOICES)
                states = {self._current(track, "compilation") for track in self.tracks}
                self.compilation.setToolTip("Actuellement : " + ("oui" if states == {"1"} else "non" if states == {""}
                                                                 else "variable selon les pistes"))
                form.addRow(FIELD_LABELS["compilation"], self.compilation)
            forms.addLayout(form, 1)
        top.addLayout(forms, 3)
        top.addWidget(self._artwork_box(), 1)
        layout.addLayout(top)
        self.table = QTableWidget(len(self.tracks), 2 + len(TRACK_FIELDS))
        self.table.setHorizontalHeaderLabels(["Fichier", *(FIELD_LABELS[key] for key in TRACK_FIELDS), "Paroles"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.lyrics = {track.path: self._current(track, "lyrics") for track in self.tracks}
        for row, track in enumerate(self.tracks):
            name = QTableWidgetItem(Path(track.path).name)
            name.setFlags(name.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name.setToolTip(track.path)
            self.table.setItem(row, 0, name)
            for column, key in enumerate(TRACK_FIELDS, 1):
                self.table.setItem(row, column, QTableWidgetItem(self._current(track, key)))
            self._show_lyrics(row)
        self.table.cellDoubleClicked.connect(self._edit_lyrics)
        self.table.resizeColumnsToContents()
        for column, minimum in ((0, 170), (1, 60), (2, 50), (3, 220), (4, 140), (5, 130), (6, 130), (7, 170), (8, 120)):
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

    def _current(self, track, key) -> str:
        if key in self.live.get(track.path, {}):
            return self.live[track.path][key]
        value = track.value(key)
        if key in ("tracknumber", "discnumber"):
            return value.split("/", 1)[0].strip()  # le total a son propre champ
        return value

    # Pochette
    def _artwork_box(self):
        box = QGroupBox("Pochette")
        layout = QVBoxLayout(box)
        self.cover_preview = QLabel()
        self.cover_preview.setFixedSize(180, 180)
        self.cover_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_preview.setStyleSheet("border:1px solid #3a4652")
        layout.addWidget(self.cover_preview, 0, Qt.AlignmentFlag.AlignHCenter)
        self.cover_label = QLabel()
        self.cover_label.setWordWrap(True)
        layout.addWidget(self.cover_label)
        choose = QPushButton("Choisir une image…")
        choose.clicked.connect(lambda: self.choose_image())
        layout.addWidget(choose)
        self.musicbrainz_cover = QPushButton("Pochette MusicBrainz…")
        self.musicbrainz_cover.setEnabled(bool(self.artworks))
        self.musicbrainz_cover.setToolTip("Services → Consulter les pochettes, puis rouvrir l’éditeur."
                                          if not self.artworks else "Image téléchargée depuis Cover Art Archive.")
        self.musicbrainz_cover.clicked.connect(lambda: self.choose_musicbrainz())
        layout.addWidget(self.musicbrainz_cover)
        remove = QPushButton("Retirer la pochette")
        remove.clicked.connect(lambda: self.set_image(None, removed=True))
        layout.addWidget(remove)
        self.cover_file = QCheckBox("Écrire aussi cover.jpg dans le dossier")
        self.cover_file.setChecked(True)
        self.cover_file.setToolTip("Lu par Jellyfin et Lidarr. Remplace cover.jpg (ou cover.png) existant ; annulable.")
        layout.addWidget(self.cover_file)
        layout.addStretch(1)
        self.image_data: bytes | None = None
        self.image_removed = False
        current = {self._current(track, ARTWORK) for track in self.tracks}
        self._preview_existing()
        self.cover_label.setText("Pochette actuelle" if current - {""} else "Aucune pochette intégrée")
        return box

    def _preview_existing(self):
        from .tagwriter import _Tags
        for track in self.tracks[:1]:
            try:
                covers = _Tags(Path(track.path)).covers()
            except Exception:
                covers = []
            if covers:
                self._show(covers[0][1])

    def _show(self, data: bytes | None):
        if data is None:
            self.cover_preview.clear()
            return
        image = QPixmap()
        image.loadFromData(data)
        if not image.isNull():
            self.cover_preview.setPixmap(image.scaled(176, 176, Qt.AspectRatioMode.KeepAspectRatio,
                                                      Qt.TransformationMode.SmoothTransformation))

    def set_image(self, data: bytes | None, removed: bool = False, source: str = ""):
        from .tagfields import image_reference
        if data is not None:
            image_reference(data)  # valide JPEG/PNG et taille, sinon ValueError
            pixmap = QPixmap()
            pixmap.loadFromData(data)
            size = f"{pixmap.width()}×{pixmap.height()}" if not pixmap.isNull() else "taille inconnue"
            self.cover_label.setText(f"Nouvelle pochette : {source or 'image choisie'} ({size}, {len(data) // 1024} Kio)")
        else:
            self.cover_label.setText("La pochette sera retirée.")
        self.image_data, self.image_removed = data, removed
        self._show(data)

    def choose_image(self, path=None):
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Image de pochette", self.album.path, "Images (*.jpg *.jpeg *.png)")
            if not path:
                return
        try:
            self.set_image(Path(path).read_bytes(), source=Path(path).name)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "Pochette", str(exc))

    def choose_musicbrainz(self, index=None):
        if not self.artworks:
            return
        if index is None:
            labels = [f"{position}. {Path(item.image_path).name} ({'face' if item.front else 'image'})"
                      for position, item in enumerate(self.artworks, 1)]
            label, accepted = QInputDialog.getItem(self, "Pochette MusicBrainz", "Image", labels, 0, False)
            if not accepted or label not in labels:
                return
            index = labels.index(label)
        self.choose_image(self.artworks[index].image_path)

    # Paroles
    def _show_lyrics(self, row):
        text = self.lyrics.get(self.tracks[row].path, "")
        item = QTableWidgetItem((text.splitlines()[0][:40] + "…") if text else "")
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        item.setToolTip("Double-cliquez pour modifier les paroles.")
        self.table.setItem(row, 1 + len(TRACK_FIELDS), item)

    def _edit_lyrics(self, row, column, text=None):
        if column != 1 + len(TRACK_FIELDS):
            return
        track = self.tracks[row]
        if text is None:
            text, accepted = QInputDialog.getMultiLineText(self, "Paroles", Path(track.path).name,
                                                           self.lyrics.get(track.path, ""))
            if not accepted:
                return
        self.lyrics[track.path] = text.strip()
        self._show_lyrics(row)

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
        choice = self.compilation.currentText()
        if choice != COMPILATION_CHOICES[0]:
            values["compilation"] = "1" if choice == "Oui" else ""
        if self.image_data is not None or self.image_removed:
            from .tagfields import image_reference
            values[ARTWORK] = image_reference(self.image_data) if self.image_data is not None else ""
        return values

    def track_values(self) -> dict[str, dict[str, str]]:
        _, mb_tracks = (proposals_to_values(self.proposals, self.album) if self.proposals_applied else ({}, {}))
        result = {}
        for row, track in enumerate(self.tracks):
            # Une cellule inchangée ne masque pas la valeur saisie pour tout l'album (ex. compositeur).
            values = {key: self.table.item(row, column).text() for column, key in enumerate(TRACK_FIELDS, 1)
                      if key not in self.album_inputs or self.table.item(row, column).text() != self._current(track, key)}
            values["lyrics"] = self.lyrics.get(track.path, "")
            # Champs d'album proposés piste par piste (valeurs différentes) : conservés.
            for key, value in mb_tracks.get(track.path, {}).items():
                if key in self.album_inputs and key not in self.touched:
                    values[key] = value
            result[track.path] = values
        return result

    def edits(self) -> list[FileEdit]:
        from .tagwriter import cover_file_edit
        edits = plan_album_edits(self.album, self.album_values(), self.track_values(), self.live)
        if self.cover_file.isChecked() and (self.image_data is not None or self.image_removed):
            from .tagfields import image_reference
            edits.append(cover_file_edit(self.album, image_reference(self.image_data)
                                         if self.image_data is not None else None))
        return edits


class TagPreviewDialog(QDialog):
    """Différences avant/après ; l'écriture demande encore ce bouton explicite."""

    def __init__(self, edits: list[FileEdit], parent=None, linked: int = 0):
        super().__init__(parent)
        self.setWindowTitle("Aperçu des modifications de tags")
        self.resize(900, 520)
        layout = QVBoxLayout(self)
        rows = describe(edits)
        summary = QLabel(f"{len(rows)} modification(s) sur {len(edits)} fichier(s). Les valeurs d’origine sont "
                         "enregistrées dans le journal avant l’écriture ; la modification pourra être annulée "
                         "depuis la page Journal des tags."
                         + (f"\n\n⚠ {linked} fichier(s) liés physiquement (qBittorrent, Lidarr) sont exclus : "
                            "modifier leurs tags modifierait aussi la copie partagée et casserait le partage."
                            if linked else ""))
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
