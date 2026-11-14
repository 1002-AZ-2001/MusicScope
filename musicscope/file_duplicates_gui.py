"""Page « Fichiers en double » : critères, filtres, liste à cocher et quarantaine."""
from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path

from PySide6.QtCore import QUrl, Qt
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMenu,
    QMessageBox, QPushButton, QSpinBox, QToolButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from . import file_duplicates as fd
from .gui import byte_size
from .organizer import QUARANTINE_DIR

DEFAULT_CRITERIA = {fd.CONTENT}
CRITERIA_HELP = {
    fd.CONTENT: "Octet pour octet (SHA-256) : certain, quel que soit le nom.",
    fd.SONG: "Même artiste et même titre (tags), durée à ±2 s : trouve un même morceau en FLAC et en MP3.",
    fd.NAME: "Même nom de fichier, sans tenir compte de la casse.",
    fd.SIZE: "Même taille en octets.",
    fd.DATE: "Même date de modification, à la seconde près.",
    fd.FINGERPRINT: "Même enregistrement d’après le son, même si les tags diffèrent. Lent ; nécessite fpcalc.",
}
COLUMNS = ["Nom", "Dossier", "Taille", "Modifié", "Format", "Débit", "Durée"]


def _extensions(text: str) -> frozenset[str]:
    return frozenset("." + part.strip().lstrip(".").casefold() for part in text.replace(";", ",").split(",") if part.strip())


class FileDuplicatesPage(QWidget):
    def __init__(self, window):
        super().__init__()
        self.window = window
        self.groups: list[fd.FileGroup] = []
        self.examined = 0
        self.records: dict[str, fd.FileRecord] = {}
        self.items: dict[str, QTreeWidgetItem] = {}
        layout = QVBoxLayout(self)
        intro = QLabel("Cochez les critères qui doivent tous concorder, lancez la recherche, puis cochez les fichiers à "
                       f"retirer. Ils sont déplacés dans « {QUARANTINE_DIR} » (restaurable) ; seul « Vider la "
                       "quarantaine » supprime définitivement.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        options = QHBoxLayout()
        criteria_box = QGroupBox("Critères (tous doivent concorder)")
        criteria_layout = QGridLayout(criteria_box)
        self.criteria_boxes: dict[str, QCheckBox] = {}
        for index, criterion in enumerate(fd.CRITERIA):
            box = QCheckBox(criterion)
            box.setChecked(criterion in DEFAULT_CRITERIA)
            box.setToolTip(CRITERIA_HELP[criterion])
            self.criteria_boxes[criterion] = box
            criteria_layout.addWidget(box, index // 2, index % 2)
        options.addWidget(criteria_box, 3)
        filters_box = QGroupBox("Filtres")
        filters = QGridLayout(filters_box)
        self.min_size = QSpinBox()
        self.min_size.setRange(0, 10_000_000)
        self.min_size.setValue(1)
        self.min_size.setSuffix(" Kio")
        self.min_size.setToolTip("Taille minimale ; les fichiers vides sont toujours ignorés.")
        self.max_size = QSpinBox()
        self.max_size.setRange(0, 1_000_000)
        self.max_size.setSuffix(" Mio")
        self.max_size.setSpecialValueText("sans limite")
        self.include = QLineEdit()
        self.include.setPlaceholderText("toutes (ex. flac, mp3, jpg)")
        self.exclude = QLineEdit()
        self.exclude.setPlaceholderText("aucune (ex. nfo, log)")
        self.folders = QLineEdit()
        self.folders.setPlaceholderText("dossiers relatifs à la racine, séparés par ;")
        self.skip_hidden = QCheckBox("Ignorer les fichiers et dossiers cachés")
        self.skip_hidden.setChecked(True)
        for row, (label, widget) in enumerate((("Taille min.", self.min_size), ("Taille max.", self.max_size),
                                               ("Extensions incluses", self.include), ("Extensions exclues", self.exclude),
                                               ("Dossiers exclus", self.folders))):
            filters.addWidget(QLabel(label), row, 0)
            filters.addWidget(widget, row, 1)
        filters.addWidget(self.skip_hidden, 5, 0, 1, 2)
        options.addWidget(filters_box, 2)
        layout.addLayout(options)
        search_row = QHBoxLayout()
        self.search_button = QPushButton("Rechercher les fichiers en double")
        self.search_button.setObjectName("primary")
        self.search_button.clicked.connect(lambda: self.search())
        search_row.addWidget(self.search_button)
        self.result_label = QLabel("Aucune recherche lancée.")
        search_row.addWidget(self.result_label, 1)
        layout.addLayout(search_row)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLUMNS))
        self.tree.setHeaderLabels(COLUMNS)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.context_menu)
        self.tree.itemChanged.connect(lambda *_: self.update_summary())
        layout.addWidget(self.tree, 1)

        actions = QHBoxLayout()
        self.selection_button = QToolButton()
        self.selection_button.setText("Sélection")
        self.selection_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.selection_button)
        for title, callback in (
            ("Garder le meilleur de chaque groupe", lambda: self.apply_selection(fd.select_all_but_best(self.groups))),
            ("Cocher les fichiers avec perte quand un sans-perte existe",
             lambda: self.apply_selection(fd.select_lossy_when_lossless(self.groups))),
            ("Garder le plus récent de chaque groupe", lambda: self.apply_selection(fd.select_keep_by_date(self.groups, True))),
            ("Garder le plus ancien de chaque groupe", lambda: self.apply_selection(fd.select_keep_by_date(self.groups, False))),
            ("Tout décocher", lambda: self.apply_selection(set())),
        ):
            menu.addAction(title).triggered.connect(lambda checked=False, callback=callback: callback())
        self.selection_button.setMenu(menu)
        self.quarantine_button = QPushButton("Mettre la sélection en quarantaine…")
        self.quarantine_button.clicked.connect(lambda: self.quarantine())
        self.restore_button = QPushButton("Restaurer…")
        self.restore_button.clicked.connect(lambda: self.restore())
        self.empty_button = QPushButton("Vider la quarantaine…")
        self.empty_button.clicked.connect(lambda: self.empty())
        self.summary = QLabel("")
        for widget in (self.selection_button, self.quarantine_button, self.restore_button, self.empty_button):
            actions.addWidget(widget)
        actions.addWidget(self.summary, 1)
        layout.addLayout(actions)
        window.buttons += [self.search_button, self.quarantine_button, self.restore_button, self.empty_button]

    # Recherche
    def options(self) -> fd.ScanOptions:
        criteria = frozenset(name for name, box in self.criteria_boxes.items() if box.isChecked())
        folders = tuple(part.strip() for part in self.folders.text().split(";") if part.strip())
        return fd.ScanOptions(criteria, min_size=max(1, self.min_size.value() * 1024),
                              max_size=self.max_size.value() * 2**20 or None,
                              include_extensions=_extensions(self.include.text()),
                              exclude_extensions=_extensions(self.exclude.text()),
                              exclude_folders=folders, skip_hidden=self.skip_hidden.isChecked())

    def search(self, fingerprinter=None):
        window = self.window
        if not window.root:
            window.state.setText("Choisissez d’abord la racine de la médiathèque.")
            return
        try:
            options = self.options()
        except ValueError as exc:
            window.state.setText(str(exc))
            return
        root = window.root
        tracks = {track.path: track for album in window.inventory.albums for track in album.tracks}

        def operation(cancel, progress):
            from .cache import Cache
            printer = fingerprinter
            owned = None
            if fd.FINGERPRINT in options.criteria and printer is None:
                from .recognition import Fingerprinter
                owned = printer = Fingerprinter(window.cache_dir / "fingerprints.sqlite3")
                printer._find_executable(cancel)  # échoue tôt si fpcalc manque
            try:
                records = fd.collect_files(root, options, tracks, cancel, progress)
                with Cache(window.cache_dir / "inventory.sqlite3") as cache:
                    return records, fd.find_duplicates(records, options, cache, printer, cancel, progress)
            finally:
                if owned is not None:
                    owned.close()

        window.run_task("Recherche des fichiers en double…", operation, self.show_results)

    def show_results(self, result):
        records, groups = result
        self.examined = len(records)
        self.render(groups)
        self.window.state.setText("Recherche terminée. Utilisez « Sélection » ou cochez les fichiers à retirer.")

    def render(self, groups):
        """Reconstruit toute la liste : aucun élément Qt n'est réutilisé après suppression."""
        self.groups = groups
        self.records = {record.path: record for group in groups for record in group.files}
        self.items = {}
        self.tree.blockSignals(True)
        self.tree.clear()
        root = self.window.root.rstrip(os.sep)
        for number, group in enumerate(groups, 1):
            wasted = sum(record.size for record in group.files) - max(record.size for record in group.files)
            parent = QTreeWidgetItem([f"Groupe {number} — {len(group.files)} fichiers", " + ".join(group.criteria),
                                      byte_size(wasted), "", "", "", ""])
            parent.setFirstColumnSpanned(False)
            parent.setForeground(0, QColor("#80cbc4"))
            parent.setToolTip(2, "Espace récupérable en gardant la plus grosse copie")
            if group.shared_inodes:
                parent.setToolTip(0, "Certains chemins désignent le même fichier physique (lien) : aucun espace libéré.")
            best = group.best()
            for record in group.files:
                folder = record.folder[len(root) + 1:] if record.folder.startswith(root + os.sep) else record.folder
                item = QTreeWidgetItem([
                    record.name, folder or ".", byte_size(record.size),
                    datetime.fromtimestamp(record.mtime_ns / 1e9).strftime("%d/%m/%Y %H:%M"), record.codec,
                    f"{record.bitrate} kb/s" if record.bitrate else "", f"{record.duration:.1f} s" if record.duration else ""])
                item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                item.setCheckState(0, Qt.CheckState.Unchecked)
                item.setData(0, Qt.ItemDataRole.UserRole, record.path)
                item.setToolTip(0, record.path)
                item.setToolTip(1, record.folder)
                if record is best:
                    item.setForeground(0, QColor("#8fd1a8"))
                    item.setToolTip(0, record.path + "\nMeilleure copie de ce groupe")
                parent.addChild(item)
                self.items[record.path] = item
            self.tree.addTopLevelItem(parent)
        self.tree.expandAll()
        self.tree.blockSignals(False)
        for column, width in enumerate((280, 320, 90, 130, 80, 80, 70)):
            self.tree.setColumnWidth(column, width)
        wasted = sum(sum(r.size for r in g.files) - max(r.size for r in g.files) for g in groups)
        self.result_label.setText(f"{self.examined} fichiers examinés • {len(groups)} groupes • "
                                  f"{byte_size(wasted)} récupérables au maximum")
        self.window.set_page_count(self.window.file_duplicates_index, len(groups))
        self.update_summary()

    # Sélection
    def selected(self) -> set[str]:
        return {path for path, item in self.items.items() if item.checkState(0) == Qt.CheckState.Checked}

    def apply_selection(self, paths: set[str]):
        self.tree.blockSignals(True)
        for path, item in self.items.items():
            item.setCheckState(0, Qt.CheckState.Checked if path in paths else Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        self.update_summary()

    def set_folder(self, folder: str, checked: bool):
        self.tree.blockSignals(True)
        for path, item in self.items.items():
            if path.startswith(folder.rstrip(os.sep) + os.sep):
                item.setCheckState(0, Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self.tree.blockSignals(False)
        self.update_summary()

    def update_summary(self):
        selected = self.selected()
        size = sum(self.records[path].size for path in selected)
        full = fd.fully_selected(self.groups, selected)
        text = f"{len(selected)} fichier(s) coché(s) • {byte_size(size)}"
        if full:
            text += f" • ⚠ {len(full)} groupe(s) entièrement coché(s)"
        self.summary.setText(text)

    def context_menu(self, position):
        item = self.tree.itemAt(position)
        path = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if not path:
            return
        folder = os.path.dirname(path)
        menu = QMenu(self)
        menu.addAction("Cocher tous les fichiers de ce dossier").triggered.connect(lambda: self.set_folder(folder, True))
        menu.addAction("Décocher tous les fichiers de ce dossier").triggered.connect(lambda: self.set_folder(folder, False))
        parent = os.path.dirname(folder)
        if parent.startswith(self.window.root):
            menu.addAction(f"Cocher tout ce qui est sous « {os.path.basename(parent)} »").triggered.connect(
                lambda: self.set_folder(parent, True))
        menu.addSeparator()
        menu.addAction("Ouvrir le dossier").triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(folder)))
        menu.exec(self.tree.viewport().mapToGlobal(position))

    # Actions
    def quarantine(self, confirm=None):
        window = self.window
        selected = self.selected()
        if not selected:
            window.state.setText("Cochez d’abord les fichiers à retirer.")
            return
        full = fd.fully_selected(self.groups, selected)
        if full:
            window.show_error(f"Refusé : {len(full)} groupe(s) ont toutes leurs copies cochées. Gardez au moins un "
                              "fichier par groupe (menu Sélection → Garder le meilleur).")
            return
        records = [self.records[path] for path in sorted(selected)]
        size = sum(record.size for record in records)
        if confirm is None:
            confirm = QMessageBox.question(self, "Mettre en quarantaine",
                f"Déplacer {len(records)} fichier(s) ({byte_size(size)}) vers :\n{Path(window.root) / QUARANTINE_DIR}\n\n"
                "Si qBittorrent partage encore ces fichiers, le partage sera interrompu. Un fichier lié physiquement "
                "ne libère pas d’espace tant que l’autre lien existe.\n\nUn journal permet de tout restaurer.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
        if not confirm:
            return
        root, journal_dir = window.root, window.cache_dir / "journals"

        def done(result):
            journal, moved, failed = result
            gone = set(moved)
            remaining = [fd.FileGroup([r for r in group.files if r.path not in gone], group.criteria)
                         for group in self.groups]
            self.render([group for group in remaining if len(group.files) > 1])
            message = f"{len(moved)} fichier(s) en quarantaine. Journal : {journal}"
            if failed:
                window.show_error(message + " — Échecs : " + " | ".join(f"{Path(p).name} : {e}" for p, e in failed))
            else:
                window.state.setText(message)
            if moved:
                window.requires_rescan = True

        window.run_task("Mise en quarantaine des fichiers cochés…",
                        lambda cancel, progress: fd.quarantine_files(records, root, journal_dir, cancel, progress), done)

    def restore(self, journal_path=None):
        window = self.window
        if journal_path is None:
            journal_path, _ = QFileDialog.getOpenFileName(self, "Journal de quarantaine à restaurer",
                                                          str(window.cache_dir / "journals"), "Journal (*.json)")
            if not journal_path:
                return
        try:
            fd.read_file_journal(journal_path)
        except (OSError, ValueError) as exc:
            # Journal d'album (rangement ou doublons d'albums) : restauration habituelle.
            if "journal de fichiers" in str(exc):
                window.restore_organization(journal_path)
                return
            window.show_error(f"Journal illisible : {exc}")
            return

        def done(result):
            restored, failed = result
            message = f"{len(restored)} fichier(s) restauré(s). Relancez la recherche pour actualiser la liste."
            if failed:
                window.show_error(message + " — Échecs : " + " | ".join(f"{Path(p).name} : {e}" for p, e in failed))
            else:
                window.state.setText(message)
            window.requires_rescan = True

        window.run_task("Restauration des fichiers…",
                        lambda cancel, progress: fd.restore_file_journal(journal_path, cancel, progress), done)

    def empty(self, confirm=None):
        window = self.window
        if not window.root:
            return
        folder, count, size = fd.quarantine_summary(window.root)
        if not count:
            window.state.setText("La quarantaine est vide.")
            return
        if confirm is None:
            first = QMessageBox.question(self, "Vider la quarantaine",
                f"Supprimer définitivement {count} fichier(s) ({byte_size(size)}) de :\n{folder}\n\n"
                "Cette suppression est IRRÉVERSIBLE : les journaux ne pourront plus rien restaurer.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            confirm = first == QMessageBox.StandardButton.Yes and QMessageBox.warning(
                self, "Dernière confirmation", f"Confirmer la suppression définitive de {count} fichier(s) ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
        if not confirm:
            return
        root = window.root

        def done(result):
            removed, freed = result
            window.state.setText(f"Quarantaine vidée : {removed} fichier(s), {byte_size(freed)} supprimés définitivement.")

        window.run_task("Suppression définitive du contenu de la quarantaine…",
                        lambda cancel, progress: fd.empty_quarantine(root, confirmed=True), done, cancellable=False)
