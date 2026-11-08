"""Interface française ; toutes les opérations longues tournent hors du fil Qt."""
from __future__ import annotations

import html
import json
import threading
from pathlib import Path

from PySide6.QtCore import QSortFilterProxyModel, Qt, QThread, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QProgressBar, QPushButton,
    QSplitter, QTabWidget, QTableView, QTextBrowser, QVBoxLayout, QWidget,
)

from .audit import audit_album
from .cache import Cache
from .comparison import candidates, compare_albums, exact_file_groups
from .export import export_csv
from .models import Inventory
from .musicbrainz import MusicBrainzClient
from .paths import check_cache_outside_library
from .scanner import ScanCancelled, scan


def escaped(value) -> str:
    return html.escape(str(value))


class Task(QThread):
    result = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int, str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.cancel = threading.Event()

    def run(self):
        try:
            self.result.emit(self.operation(self.cancel, self.progress.emit))
        except ScanCancelled:
            self.failed.emit("Opération annulée ; l’inventaire précédent est conservé.")
        except Exception as exc:
            self.failed.emit(f"{type(exc).__name__} : {exc}")


def make_table(headers):
    table = QTableView()
    model = QStandardItemModel(0, len(headers))
    model.setHorizontalHeaderLabels(headers)
    proxy = QSortFilterProxyModel(table)
    proxy.setSourceModel(model)
    proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    proxy.setFilterKeyColumn(-1)
    table.setModel(proxy)
    table.setSortingEnabled(True)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.verticalHeader().hide()
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    table.horizontalHeader().setStretchLastSection(True)
    table.setAlternatingRowColors(True)
    return table, model, proxy


def append_row(model, values, identity=None):
    items = []
    for value in values:
        item = QStandardItem()
        item.setData(value, Qt.ItemDataRole.DisplayRole)
        item.setToolTip(str(value))
        items.append(item)
    if identity is not None:
        items[0].setData(identity, Qt.ItemDataRole.UserRole)
    model.appendRow(items)


class MainWindow(QMainWindow):
    def __init__(self, cache_dir: Path):
        super().__init__()
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.inventory = Inventory(root="")
        self.matches = []
        self.exact_groups = []
        self.verifications = {}
        self.task = None
        self.result_callback = None
        self.root = ""
        self.setWindowTitle("MusicScope — Audit de votre médiathèque")
        self.resize(1320, 860)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(22, 18, 22, 18)
        heading = QLabel("MusicScope  <span style='color:#80cbc4;font-size:14px'>  AUDIT MUSICAL</span>")
        heading.setStyleSheet("font-size:27px;font-weight:700")
        layout.addWidget(heading)
        layout.addWidget(QLabel("Inventorier • comprendre les ressemblances • vérifier les métadonnées"))
        row = QHBoxLayout()
        self.root_label = QLineEdit()
        self.root_label.setPlaceholderText("Choisissez votre dossier musical local ou réseau…")
        self.root_label.setReadOnly(True)
        row.addWidget(self.root_label, 1)
        self.choose_button = QPushButton("Choisir le dossier")
        self.choose_button.clicked.connect(self.choose_root)
        self.scan_button = QPushButton("Analyser")
        self.scan_button.clicked.connect(self.start_scan)
        row.addWidget(self.choose_button)
        row.addWidget(self.scan_button)
        layout.addLayout(row)
        controls = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filtrer par artiste, album, chemin ou constat…")
        controls.addWidget(self.filter, 1)
        self.open_button = QPushButton("Ouvrir le dossier")
        self.open_button.clicked.connect(self.open_folder)
        self.compare_button = QPushButton("Comparer 2 albums")
        self.compare_button.clicked.connect(self.compare_selected)
        self.exact_button = QPushButton("Recherche exacte SHA-256")
        self.exact_button.clicked.connect(self.start_exact)
        self.mb_button = QPushButton("Vérifier avec MusicBrainz")
        self.mb_button.clicked.connect(self.start_musicbrainz)
        self.export_button = QPushButton("Exporter CSV")
        self.export_button.clicked.connect(self.export)
        for button in (self.open_button, self.compare_button, self.exact_button, self.mb_button, self.export_button):
            controls.addWidget(button)
        layout.addLayout(controls)
        self.summary = QLabel("Aucun inventaire. La médiathèque est ouverte en lecture seule.")
        layout.addWidget(self.summary)
        self.tabs = QTabWidget()
        self.album_table, self.album_model, self.album_proxy = make_table(
            ["Artiste d’album", "Album", "Pistes", "Codecs", "Édition", "Constats", "Dossier"])
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.album_table)
        self.album_details = QTextBrowser()
        splitter.addWidget(self.album_details)
        splitter.setSizes([360, 200])
        self.tabs.addTab(splitter, "Inventaire")
        self.match_table, self.match_model, self.match_proxy = make_table(
            ["Statut", "Album A", "Album B", "Motifs"])
        comparison_splitter = QSplitter(Qt.Orientation.Vertical)
        comparison_splitter.addWidget(self.match_table)
        detail_splitter = QSplitter()
        self.left_details = QTextBrowser()
        self.right_details = QTextBrowser()
        detail_splitter.addWidget(self.left_details)
        detail_splitter.addWidget(self.right_details)
        comparison_splitter.addWidget(detail_splitter)
        comparison_splitter.setSizes([350, 230])
        self.tabs.addTab(comparison_splitter, "Rapprochements")
        self.exact_details = QTextBrowser()
        self.tabs.addTab(self.exact_details, "Fichiers identiques")
        self.mb_details = QTextBrowser()
        self.mb_details.setOpenExternalLinks(True)
        self.tabs.addTab(self.mb_details, "MusicBrainz")
        self.log_details = QTextBrowser()
        self.tabs.addTab(self.log_details, "Erreurs de lecture")
        layout.addWidget(self.tabs, 1)
        bottom = QHBoxLayout()
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.cancel_button = QPushButton("Annuler")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_task)
        bottom.addWidget(self.progress_bar, 1)
        bottom.addWidget(self.cancel_button)
        layout.addLayout(bottom)
        self.state = QLabel("Prêt — aucun fichier musical ne sera modifié.")
        self.state.setWordWrap(True)
        layout.addWidget(self.state)
        self.setCentralWidget(body)
        self.filter.textChanged.connect(self.album_proxy.setFilterFixedString)
        self.filter.textChanged.connect(self.match_proxy.setFilterFixedString)
        self.album_table.selectionModel().selectionChanged.connect(self.show_album)
        self.match_table.selectionModel().selectionChanged.connect(self.show_match)
        self.buttons = [self.choose_button, self.scan_button, self.open_button, self.compare_button,
                        self.exact_button, self.mb_button, self.export_button]
        self.setStyleSheet("""
            QMainWindow {background:#17212b;color:#e9eff5} QWidget {font-size:13px}
            QLabel {color:#e9eff5} QPushButton {padding:8px 12px;background:#284453;color:#f4f8fb;
                border:1px solid #426371;border-radius:5px} QPushButton:hover {background:#346273}
            QPushButton:disabled {color:#82939e;background:#24313b;border-color:#33414b}
            QLineEdit {padding:9px;border:1px solid #445764;border-radius:5px;background:#202f3a;color:#eef4fa}
            QTableView,QTextBrowser {background:#f9fbfc;alternate-background-color:#edf3f6;color:#20313c;
                border:0;selection-background-color:#227c8a;selection-color:white}
            QHeaderView::section {padding:8px;background:#dfe9ef;color:#20313c;border:0;font-weight:600}
            QTabWidget::pane {border:1px solid #445764} QTabBar::tab {padding:10px 18px;
                background:#243744;color:#b9cbd6} QTabBar::tab:selected {background:#147d88;color:white}
            QProgressBar {background:#243744;color:white;border:0;border-radius:4px;text-align:center}
            QProgressBar::chunk {background:#239ba2;border-radius:4px}
        """)
        self.album_details.setHtml("<h2>Votre bibliothèque, sans modification</h2><p>Choisissez une racine, puis lancez l’analyse. "
                                   "Sélectionnez deux lignes avec Ctrl pour comparer deux dossiers.</p>")
        self.exact_details.setHtml("<h2>Recherche exacte à la demande</h2><p>Les fichiers sont préfiltrés par taille, "
                                   "puis lus en flux pour calculer leur SHA-256, indépendamment des noms.</p>")
        self.mb_details.setHtml("<h2>Vérification MusicBrainz</h2><p>Sélectionnez un album dans l’inventaire, puis cliquez sur "
                                "« Vérifier avec MusicBrainz ». Aucun tag n’est écrit.</p>")
        self.load_preferences()

    def load_preferences(self):
        try:
            self.root = json.loads((self.cache_dir / "preferences.json").read_text("utf-8"))["root"]
            check_cache_outside_library(self.cache_dir, Path(self.root))
            self.root_label.setText(self.root)
            cache = Cache(self.cache_dir / "inventory.sqlite3")
            try:
                saved = cache.load_inventory(self.root)
            finally:
                cache.close()
            if saved:
                self.display_inventory(saved)
                self.state.setText("Inventaire précédent chargé. Relancez l’analyse pour actualiser les fichiers.")
        except (OSError, ValueError, KeyError):
            self.root = ""

    def choose_root(self):
        root = QFileDialog.getExistingDirectory(self, "Racine de la médiathèque", self.root)
        if not root:
            return
        try:
            check_cache_outside_library(self.cache_dir, Path(root))
        except ValueError as exc:
            self.show_error(str(exc))
            return
        self.root = root
        self.root_label.setText(root)
        self.start_scan()

    def run_task(self, label, operation, on_result, cancellable=True):
        if self.task is not None:
            return
        self.state.setText(label)
        for button in self.buttons:
            button.setEnabled(False)
        self.progress_bar.setRange(0, 0)
        self.cancel_button.setEnabled(cancellable)
        task = Task(operation, self)
        self.task = task
        self.result_callback = on_result
        # Un callback Python libre n'a pas d'affinité QObject explicite. Les
        # widgets ne doivent jamais être manipulés depuis le worker.
        task.progress.connect(self.on_progress, Qt.ConnectionType.QueuedConnection)
        task.result.connect(self.receive_result, Qt.ConnectionType.QueuedConnection)
        task.failed.connect(self.show_error, Qt.ConnectionType.QueuedConnection)
        task.finished.connect(self.task_finished, Qt.ConnectionType.QueuedConnection)
        task.start()

    @Slot(object)
    def receive_result(self, result):
        try:
            if self.result_callback is not None:
                self.result_callback(result)
        except Exception as exc:
            self.show_error(f"Affichage du résultat impossible : {exc}")

    @Slot(int, int, str)
    def on_progress(self, current, total, path):
        self.progress_bar.setRange(0, total if total > 0 else 0)
        self.progress_bar.setValue(current)
        self.state.setText(f"{current}/{total} — {path}")

    def cancel_task(self):
        if self.task:
            self.task.cancel.set()
            self.state.setText("Annulation demandée ; attente de la fin de lecture en cours…")
            self.cancel_button.setEnabled(False)

    @Slot()
    def task_finished(self):
        task = self.task
        # finished est émis avant le nettoyage natif final de QThread.
        task.wait()
        task.operation = None
        self.result_callback = None
        self.task = None
        task.deleteLater()
        for button in self.buttons:
            button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)

    @Slot(str)
    def show_error(self, message):
        self.state.setText(message)
        self.log_details.append(f"<p>{escaped(message)}</p>")

    def start_scan(self):
        if not self.root:
            self.choose_root()
            return
        root = self.root
        def operation(cancel, progress):
            check_cache_outside_library(self.cache_dir, Path(root))
            cache = Cache(self.cache_dir / "inventory.sqlite3")
            try:
                result = scan(Path(root), cache, cancel=cancel, progress=progress)
                pairs = [(a, b, compare_albums(a, b)) for a, b in candidates(result.albums)]
                return result, pairs
            finally:
                cache.close()
        self.run_task("Lecture de la médiathèque…", operation, self.scan_completed)

    def scan_completed(self, result):
        inventory, pairs = result
        self.display_inventory(inventory, pairs)
        (self.cache_dir / "preferences.json").write_text(json.dumps({"root": self.root}), "utf-8")
        self.state.setText("Analyse terminée. Sélectionnez deux albums pour les comparer, ou lancez la recherche exacte.")

    def display_inventory(self, inventory, pairs=None):
        self.inventory = inventory
        self.matches = pairs or []
        self.exact_groups = []
        self.verifications = {}
        self.album_model.removeRows(0, self.album_model.rowCount())
        for index, album in enumerate(inventory.albums):
            issues = list(dict.fromkeys(album.issues + audit_album(album)))
            append_row(self.album_model, [album.value("albumartist") or album.value("artist") or "Artiste inconnu",
                album.value("album") or Path(album.path).name, len(album.tracks),
                ", ".join(sorted({t.codec for t in album.tracks})), album.value("musicbrainz_albumid") or "Indéterminée",
                " • ".join(issues) or "Aucun constat local", album.path], index)
        self.populate_matches()
        self.album_table.resizeColumnsToContents()
        for col in (0, 1, 4, 5):
            self.album_table.setColumnWidth(col, min(self.album_table.columnWidth(col), 250))
        self.log_details.setPlainText("\n".join(inventory.errors) or "Aucun fichier illisible signalé.")
        self.exact_details.setPlainText("Recherche exacte non exécutée pour cet inventaire.")
        self.mb_details.setPlainText("Vérification en ligne non exécutée pour cet inventaire.")
        self.summary.setText(f"{len(inventory.albums)} albums • {sum(len(a.tracks) for a in inventory.albums)} pistes • "
                             f"{len(self.matches)} rapprochements • {len(inventory.errors)} erreurs — {inventory.scanned_at}")

    def selected_albums(self):
        return [self.inventory.albums[self.album_proxy.index(index.row(), 0).data(Qt.ItemDataRole.UserRole)]
                for index in self.album_table.selectionModel().selectedRows()]

    def album_html(self, album):
        issues = list(dict.fromkeys(album.issues + audit_album(album)))
        lines = [f"<h2>{escaped(album.value('album') or Path(album.path).name)}</h2>",
                 f"<p><b>{escaped(album.value('albumartist'))}</b><br>{escaped(album.path)}</p>",
                 "<p>" + "<br>".join(escaped(issue) for issue in issues) + "</p>",
                 "<table border='1' cellspacing='0' cellpadding='5'><tr><th>Disque/piste</th><th>Titre</th>"
                 "<th>Artiste</th><th>Durée</th><th>Codec</th></tr>"]
        for track in album.ordered_tracks:
            duration = f"{track.duration:.2f} s" if track.duration is not None else "Inconnue"
            lines.append(f"<tr><td>{track.disc}/{track.number_in_disc}</td><td>{escaped(track.value('title'))}</td>"
                         f"<td>{escaped(track.value('artist'))}</td><td>{duration}</td><td>{escaped(track.codec)}</td></tr>")
        lines.append("</table><h3>Tags actuels par fichier</h3>")
        for track in album.ordered_tracks:
            lines.append(f"<p><b>{escaped(Path(track.path).name)}</b><br>" + "<br>".join(
                f"{escaped(key)} : {escaped('; '.join(value))}" for key, value in sorted(track.tags.items())) + "</p>")
        return "".join(lines)

    def show_album(self, *_):
        selected = self.selected_albums()
        if selected:
            self.album_details.setHtml(self.album_html(selected[0]))

    def populate_matches(self):
        self.match_model.removeRows(0, self.match_model.rowCount())
        for index, (left, right, comparison) in enumerate(self.matches):
            append_row(self.match_model, [comparison.status, left.path, right.path,
                " • ".join(comparison.reasons + comparison.differences)], index)
        self.match_table.setColumnWidth(0, 260)
        self.match_table.setColumnWidth(1, 250)
        self.match_table.setColumnWidth(2, 250)

    def show_match(self, *_):
        selected = self.match_table.selectionModel().selectedRows()
        if not selected:
            return
        index = self.match_proxy.index(selected[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        left, right, comparison = self.matches[index]
        reasons = "<h3>" + escaped(comparison.status) + "</h3><p>" + "<br>".join(
            escaped(item) for item in comparison.reasons + comparison.differences) + "</p>"
        self.left_details.setHtml(reasons + self.album_html(left))
        self.right_details.setHtml(self.album_html(right))

    def compare_selected(self):
        selected = self.selected_albums()
        if len(selected) != 2:
            self.state.setText("Sélectionnez exactement deux albums dans l’inventaire (Ctrl + clic).")
            self.tabs.setCurrentIndex(0)
            return
        left, right = selected
        def operation(cancel, progress):
            cache = Cache(self.cache_dir / "inventory.sqlite3")
            try:
                return left, right, compare_albums(left, right, cache=cache, exact=True, cancel=cancel)
            finally:
                cache.close()
        def done(result):
            self.matches.append(result)
            self.populate_matches()
            self.tabs.setCurrentIndex(1)
            for row in range(self.match_proxy.rowCount()):
                if self.match_proxy.index(row, 0).data(Qt.ItemDataRole.UserRole) == len(self.matches) - 1:
                    self.match_table.selectRow(row)
                    break
            self.state.setText("Comparaison terminée, avec contrôle des octets si les tailles concordent.")
        self.run_task("Comparaison des deux dossiers…", operation, done)

    def start_exact(self):
        if not self.inventory.albums:
            self.state.setText("Analysez d’abord une médiathèque.")
            return
        inventory = self.inventory
        def operation(cancel, progress):
            cache = Cache(self.cache_dir / "inventory.sqlite3")
            try:
                return exact_file_groups(inventory, cache, cancel=cancel, progress=progress)
            finally:
                cache.close()
        def done(groups):
            self.exact_groups = groups
            text = [f"<h2>{len(groups)} groupes de fichiers identiques octet pour octet</h2>"]
            for num, tracks in enumerate(groups, 1):
                text.append(f"<h3>Groupe {num} — {tracks[0].size:,} octets par chemin</h3><ul>" + "".join(
                    f"<li>{escaped(track.path)}</li>" for track in tracks) + "</ul>")
                identities = {(t.device, t.inode) for t in tracks if t.device is not None and t.inode is not None}
                text.append("<p>Identité physique indisponible pour au moins un chemin.</p>" if any(
                    t.device is None or t.inode is None for t in tracks) else
                    f"<p>{len(identities)} fichiers physiques pour {len(tracks)} chemins (un lien physique ne libère pas une copie entière).</p>")
            self.exact_details.setHtml("".join(text))
            self.tabs.setCurrentIndex(2)
            self.state.setText("Recherche exacte terminée ; les résultats concernent des fichiers, pas nécessairement des albums complets.")
        self.run_task("Calcul des SHA-256 des candidats de même taille…", operation, done)

    def start_musicbrainz(self):
        selected = self.selected_albums()
        if len(selected) != 1:
            self.state.setText("Sélectionnez un seul album dans l’inventaire pour MusicBrainz.")
            self.tabs.setCurrentIndex(0)
            return
        album = selected[0]
        def operation(cancel, progress):
            client = MusicBrainzClient(self.cache_dir / "musicbrainz.sqlite3")
            try:
                return client.verify_album(album)
            finally:
                client.close()
        def done(result):
            self.verifications[album.path] = result
            text = [f"<h2>{escaped(result.status)}</h2><p>{escaped(album.path)}</p><p>",
                    "<br>".join(escaped(reason) for reason in result.reasons), "</p>"]
            for candidate in result.candidates:
                text.append(f"<h3><a href='{escaped(candidate.url)}'>{escaped(candidate.title)}</a></h3>"
                    f"<p>{escaped(candidate.artist)} • {escaped(candidate.date)} • {escaped(candidate.country)} • "
                    f"{escaped(candidate.disambiguation)}<br>" + "<br>".join(escaped(r) for r in candidate.reasons) + "</p>")
            text.append("<h3>Tags actuels et propositions — aucune écriture</h3><table border='1' cellspacing='0' cellpadding='5'>"
                        "<tr><th>Fichier</th><th>Champ</th><th>Actuel</th><th>Proposé</th><th>Motif</th></tr>")
            for proposal in result.proposals:
                text.append("<tr>" + "".join(f"<td>{escaped(value)}</td>" for value in (
                    Path(proposal.path).name, proposal.field, proposal.current, proposal.proposed, proposal.reason)) + "</tr>")
            text.append("</table>")
            self.mb_details.setHtml("".join(text))
            self.tabs.setCurrentIndex(3)
            self.state.setText("Vérification MusicBrainz terminée. Les propositions sont consultatives.")
        self.run_task("Consultation MusicBrainz (requêtes limitées, attente réseau possible)…", operation, done, cancellable=False)

    def open_folder(self):
        selected = self.selected_albums()
        if selected:
            QDesktopServices.openUrl(QUrl.fromLocalFile(selected[0].path))
        elif self.root:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.root))

    def findings(self):
        rows = []
        for album in self.inventory.albums:
            for issue in list(dict.fromkeys(album.issues + audit_album(album))):
                rows.append({"catégorie": "Audit local", "dossier": album.path, "statut": "À vérifier", "motif": issue})
            for track in album.tracks:
                rows.append({"catégorie": "Inventaire", "dossier": album.path, "champ": "fichier", "actuel": track.path,
                             "motif": f"{track.codec} ; {track.size} octets ; {track.duration} secondes"})
        for left, right, comparison in self.matches:
            rows.append({"catégorie": "Comparaison", "dossier": left.path, "autre_dossier": right.path,
                         "statut": comparison.status, "motif": " | ".join(comparison.reasons + comparison.differences)})
        for group in self.exact_groups:
            for track in group:
                rows.append({"catégorie": "Fichiers identiques", "dossier": track.path, "autre_dossier": group[0].path,
                             "statut": "Fichiers identiques octet pour octet", "motif": "SHA-256 identique"})
        for path, result in self.verifications.items():
            rows.append({"catégorie": "MusicBrainz", "dossier": path, "statut": result.status, "motif": " | ".join(result.reasons)})
            for candidate in result.candidates:
                rows.append({"catégorie": "Édition candidate", "dossier": path, "statut": result.status,
                             "proposé": f"{candidate.artist} / {candidate.title} / {candidate.date} / {candidate.country}",
                             "motif": " | ".join(candidate.reasons), "lien": candidate.url})
            for p in result.proposals:
                rows.append({"catégorie": "Proposition", "dossier": p.path, "statut": result.status,
                             "champ": p.field, "actuel": p.current, "proposé": p.proposed, "motif": p.reason, "lien": p.url})
        for error in self.inventory.errors:
            rows.append({"catégorie": "Erreur", "statut": "Illisible", "motif": error})
        return rows

    def export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Exporter les constats", "MusicScope.csv", "CSV (*.csv)")
        if path:
            try:
                export_csv(Path(path), self.findings())
                self.state.setText(f"Export enregistré : {path}")
            except OSError as exc:
                self.show_error(str(exc))

    def closeEvent(self, event):
        if self.task is not None:
            self.cancel_task()
            QMessageBox.information(self, "Opération en cours", "Attendez la fin de l’opération, puis fermez la fenêtre.")
            event.ignore()
            return
        event.accept()
