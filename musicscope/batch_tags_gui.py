"""Modification des tags de plusieurs albums d'un coup ; rien n'est écrit par ce dialogue."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
    QWidget,
)

from .tagwriter import FIELD_LABELS, REPLACE_FIELDS, Replacement

TEXT_FIELDS = ("genre", "date", "albumartist", "artist", "composer", "comment")
UNCHANGED, YES, NO = "Inchangée", "Oui", "Non"


class BatchTagDialog(QDialog):
    def __init__(self, albums, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Modifier les tags de {len(albums)} albums")
        self.resize(720, 560)
        layout = QVBoxLayout(self)
        intro = QLabel(f"{len(albums)} album(s), {sum(len(album.tracks) for album in albums)} piste(s). Un champ "
                       "laissé vide n’est pas modifié ; « Vider » efface sa valeur. Un aperçu de chaque fichier "
                       "s’affiche avant l’écriture, et tout le lot pourra être annulé depuis le journal des tags.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        values = QGroupBox("Même valeur sur toutes les pistes")
        form = QFormLayout(values)
        self.inputs, self.clears = {}, {}
        for key in TEXT_FIELDS:
            field, clear = QLineEdit(), QCheckBox("Vider")
            field.setPlaceholderText("inchangé")
            clear.toggled.connect(lambda checked, field=field: (field.setEnabled(not checked),
                                                                field.clear() if checked else None))
            row = QWidget()
            line = QHBoxLayout(row)
            line.setContentsMargins(0, 0, 0, 0)
            line.addWidget(field, 1)
            line.addWidget(clear)
            form.addRow(FIELD_LABELS[key], row)
            self.inputs[key], self.clears[key] = field, clear
        self.compilation = QComboBox()
        self.compilation.addItems([UNCHANGED, YES, NO])
        form.addRow(FIELD_LABELS["compilation"], self.compilation)
        layout.addWidget(values)
        replace = QGroupBox("Rechercher / remplacer (seulement là où le texte apparaît)")
        grid = QFormLayout(replace)
        self.replace_field = QComboBox()
        for key in REPLACE_FIELDS:
            self.replace_field.addItem(FIELD_LABELS[key], key)
        self.search, self.replacement = QLineEdit(), QLineEdit()
        self.search.setPlaceholderText("texte à rechercher (laisser vide : aucun remplacement)")
        self.replacement.setPlaceholderText("remplacer par (vide : supprimer le texte trouvé)")
        self.case = QCheckBox("Respecter les majuscules")
        grid.addRow("Champ", self.replace_field)
        grid.addRow("Rechercher", self.search)
        grid.addRow("Remplacer par", self.replacement)
        grid.addRow("", self.case)
        layout.addWidget(replace)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Prévisualiser")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Annuler")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def request(self) -> tuple[dict[str, str], set[str], Replacement | None]:
        values = {key: field.text().strip() for key, field in self.inputs.items()
                  if field.text().strip() and not self.clears[key].isChecked()}
        clear = {key for key, box in self.clears.items() if box.isChecked()}
        choice = self.compilation.currentText()
        if choice == YES:
            values["compilation"] = "1"
        elif choice == NO:
            clear.add("compilation")
        replacement = None
        if self.search.text():
            replacement = Replacement(self.replace_field.currentData(), self.search.text(), self.replacement.text(),
                                      self.case.isChecked())
        return values, clear, replacement
