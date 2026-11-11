"""Parcours de revue, services facultatifs et rangement explicitement confirmé."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path

from PySide6.QtCore import QSize, QTimer, Qt, Slot
from PySide6.QtGui import QAction, QImageReader, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from .gui import MainWindow, RelativePathDelegate, append_row, escaped, make_browser, make_table
from .organizer import QUARANTINE_DIR


def byte_label(value) -> str:
    if value is None:
        return "Indéterminé"
    return f"{value:,} octets ({value / (1024 ** 2):.2f} Mio)"


class JellyfinDialog(QDialog):
    """Les identifiants restent dans ce dialogue et dans la session courante."""
    def __init__(self, root, previous=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Diagnostic Jellyfin — requêtes de consultation")
        self.resize(800, 430)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.server = QLineEdit(previous[0] if previous else "")
        self.server.setPlaceholderText("https://jellyfin.exemple.fr ou http://serveur:8096")
        self.token = QLineEdit(previous[1] if previous else "")
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Adresse du serveur", self.server)
        form.addRow("Clé API (session seulement)", self.token)
        layout.addLayout(form)
        layout.addWidget(QLabel("Correspondances explicites : chemin du catalogue → chemin de ce poste."))
        self.mapping_table = QTableWidget(0, 2)
        self.mapping_table.setHorizontalHeaderLabels(["Préfixe serveur", "Préfixe local"])
        self.mapping_table.horizontalHeader().setStretchLastSection(True)
        self.mapping_table.setColumnWidth(0, 350)
        rows = previous[2] if previous else [("/media/Musique", root)]
        for server, local in rows:
            self.add_mapping(server, local)
        layout.addWidget(self.mapping_table)
        add_button = QPushButton("Ajouter une correspondance")
        add_button.clicked.connect(lambda: self.add_mapping("", ""))
        layout.addWidget(add_button)
        layout.addWidget(QLabel("Les chemins sans correspondance restent visibles. Aucun changement du catalogue n'est envoyé."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Comparer")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Annuler")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def add_mapping(self, server, local):
        row = self.mapping_table.rowCount()
        self.mapping_table.insertRow(row)
        self.mapping_table.setItem(row, 0, QTableWidgetItem(server))
        self.mapping_table.setItem(row, 1, QTableWidgetItem(local))

    def configuration(self):
        rows = []
        for row in range(self.mapping_table.rowCount()):
            values = [self.mapping_table.item(row, col).text().strip() if self.mapping_table.item(row, col) else ""
                      for col in range(2)]
            if any(values):
                rows.append(tuple(values))
        return self.server.text().strip(), self.token.text(), rows


class AdvancedWindow(MainWindow):
    """Ajoute les outils avancés sans déplacer les cinq onglets d'audit initial."""
    def __init__(self, cache_dir: Path):
        self.extensions_ready = False
        self.historical = False
        self.requires_rescan = False
        self.pending_plan = None
        self._after_task = None
        self._jellyfin_config = None
        self.jellyfin_result = None
        self.additional_findings = []
        self.imported_session = None
        self.current_decisions = []
        self.artworks = []
        self.extension_actions = []
        super().__init__(cache_dir)
        self.setWindowTitle("MusicScope — Inventaire et revue de votre médiathèque")
        self._build_extended_tabs()
        self._build_menus()
        self.extensions_ready = True
        self.refresh_reviews()
        self.refresh_tag_journal()
        self.refresh_duplicates()

    def _build_extended_tabs(self):
        self.jellyfin_table, self.jellyfin_model, self.jellyfin_proxy = make_table(
            ["Statut", "Chemin serveur", "Chemin local", "Titre", "Identifiant catalogue", "Motif"])
        self.jellyfin_index = self.add_page(self.jellyfin_table, "Jellyfin", "Services")
        sources_page = QWidget()
        sources_layout = QVBoxLayout(sources_page)
        self.sources_details = make_browser(external_links=True)
        self.sources_details.setHtml("<h2>Enregistrements, alias et pochettes</h2><p>Utilisez le menu Services. "
                                    "Chaque consultation distante est déclenchée à votre demande ; une empreinte "
                                    "d'enregistrement ne détermine pas une édition.</p>")
        sources_layout.addWidget(self.sources_details, 1)
        self.cover_layout = QHBoxLayout()
        sources_layout.addLayout(self.cover_layout)
        self.sources_index = self.add_page(sources_page, "Sources", "Services")
        quality_page = QWidget()
        quality_layout = QVBoxLayout(quality_page)
        self.storage_label = QLabel("Estimation de l’espace : menu Revue → Estimer l’espace des copies exactes.")
        self.storage_label.setWordWrap(True)
        quality_layout.addWidget(self.storage_label)
        self.quality_filter = QComboBox()
        self.quality_filter.addItem("Tous les constats")
        quality_layout.addWidget(self.quality_filter)
        self.quality_table, self.quality_model, self.quality_proxy = make_table(
            ["Priorité", "Catégorie", "Dossier", "Constat"])
        self.quality_proxy.setFilterKeyColumn(1)
        self.quality_filter.currentIndexChanged.connect(self.filter_quality)
        quality_layout.addWidget(self.quality_table, 1)
        self.quality_index = self.add_page(quality_page, "Qualité", "Bibliothèque")
        self.decision_table, self.decision_model, self.decision_proxy = make_table(
            ["Décision", "Album A", "Album B", "Note", "Date", "Validité"])
        self.decisions_index = self.add_page(self.decision_table, "Décisions", "Doublons")
        duplicates_page = QWidget()
        duplicates_layout = QVBoxLayout(duplicates_page)
        duplicates_intro = QLabel("Albums présents plusieurs fois, y compris sous des noms de dossiers différents "
                                  "(« 2014 - Artiste - Album », « Album (2014) »…). Les copies en trop sont déplacées "
                                  f"dans « {QUARANTINE_DIR} » à la racine, avec un journal restaurable : rien n’est effacé.")
        duplicates_intro.setWordWrap(True)
        duplicates_layout.addWidget(duplicates_intro)
        self.duplicate_table, self.duplicate_model, self.duplicate_proxy = make_table(
            ["Confiance", "Album", "Copies", "Copie à garder", "Espace récupérable", "Remarque"])
        duplicates_layout.addWidget(self.duplicate_table, 2)
        self.duplicate_details = make_browser()
        duplicates_layout.addWidget(self.duplicate_details, 1)
        duplicate_buttons = QHBoxLayout()
        self.keep_button = QPushButton("Choisir la copie à garder…")
        self.keep_button.clicked.connect(lambda: self.choose_kept_copy())
        self.quarantine_button = QPushButton("Mettre en quarantaine les copies en trop…")
        self.quarantine_button.clicked.connect(lambda: self.quarantine_duplicates())
        self.restore_quarantine_button = QPushButton("Restaurer depuis la quarantaine…")
        self.restore_quarantine_button.clicked.connect(lambda: self.restore_organization())
        for button in (self.keep_button, self.quarantine_button, self.restore_quarantine_button):
            duplicate_buttons.addWidget(button)
        duplicate_buttons.addStretch(1)
        duplicates_layout.addLayout(duplicate_buttons)
        self.duplicates_index = self.add_page(duplicates_page, "Albums en double", "Doublons")
        self.duplicate_groups = []
        self.duplicate_table.selectionModel().selectionChanged.connect(self.show_duplicate_group)
        self.organizer_details = make_browser()
        self.organizer_details.setHtml("<h2>Rangement assisté</h2><p>Prévisualisez d'abord la destination d'un album "
            "avec le menu Rangement. Le déplacement demande ensuite votre confirmation et crée un journal restaurable. "
            "Les fichiers audio ne sont ni supprimés ni retagués.</p>")
        self.organizer_index = self.add_page(self.organizer_details, "Rangement", "Organisation")
        self.session_details = make_browser()
        self.session_details.setPlainText("Le menu Session exporte ou importe un rapport JSON versionné. "
                                          "Une session importée contient des preuves historiques, à actualiser par un scan.")
        self.session_index = self.add_page(self.session_details, "Session", "Organisation")
        tags_page = QWidget()
        tags_layout = QVBoxLayout(tags_page)
        tags_intro = QLabel("Chaque écriture de tags est journalisée avec les valeurs d’origine. Sélectionnez une "
                            "modification pour voir son détail ou l’annuler.")
        tags_intro.setWordWrap(True)
        tags_layout.addWidget(tags_intro)
        self.tag_batch_table, self.tag_batch_model, self.tag_batch_proxy = make_table(
            ["N°", "Date", "Album", "Fichiers écrits", "État"])
        self.tag_batch_table.setSelectionMode(self.tag_batch_table.SelectionMode.SingleSelection)
        tags_layout.addWidget(self.tag_batch_table, 2)
        self.tag_details = make_browser()
        tags_layout.addWidget(self.tag_details, 1)
        tag_buttons = QHBoxLayout()
        self.edit_tags_button = QPushButton("Modifier les tags de l’album…")
        self.edit_tags_button.clicked.connect(lambda: self.edit_tags())
        self.undo_tags_button = QPushButton("Annuler la modification sélectionnée…")
        self.undo_tags_button.clicked.connect(lambda: self.undo_tags())
        tag_buttons.addWidget(self.edit_tags_button)
        tag_buttons.addWidget(self.undo_tags_button)
        tag_buttons.addStretch(1)
        tags_layout.addLayout(tag_buttons)
        self.tags_index = self.add_page(tags_page, "Journal des tags", "Organisation")
        self.tag_batch_table.selectionModel().selectionChanged.connect(self.show_tag_batch)
        self.buttons += [self.edit_tags_button, self.undo_tags_button, self.keep_button, self.quarantine_button,
                         self.restore_quarantine_button]
        delegate = RelativePathDelegate(self, self)
        self.extended_path_delegate = delegate
        self.quality_table.setItemDelegateForColumn(2, delegate)
        self.decision_table.setItemDelegateForColumn(1, delegate)
        self.decision_table.setItemDelegateForColumn(2, delegate)
        self.jellyfin_table.setItemDelegateForColumn(2, delegate)
        self.tag_batch_table.setItemDelegateForColumn(2, delegate)

    def _action(self, menu, title, callback):
        action = QAction(title, self)
        action.triggered.connect(lambda checked=False: callback())
        menu.addAction(action)
        self.extension_actions.append(action)
        return action

    def _build_menus(self):
        sources = self.add_menu("Services")
        self._action(sources, "Diagnostic Jellyfin : configurer et comparer le catalogue…", self.start_jellyfin)
        sources.addSeparator()
        self._action(sources, "Empreinte Chromaprint et recherche AcoustID…", self.start_recognition)
        self._action(sources, "Consulter ou comparer les pochettes…", self.fetch_artworks)
        self._action(sources, "Rechercher les alias de l’artiste…", self.resolve_alias)
        review = self.add_menu("Revue")
        self._action(review, "Enregistrer une décision pour deux albums…", self.record_decision)
        self._action(review, "Actualiser les décisions enregistrées", self.refresh_decisions)
        self._action(review, "Afficher les constats prioritaires", lambda: self.tabs.setCurrentIndex(self.quality_index))
        self._action(review, "Estimer l’espace des copies exactes", self.estimate_storage)
        review.addSeparator()
        self._action(review, "Albums en double…", lambda: self.tabs.setCurrentIndex(self.duplicates_index))
        self._action(review, "Mettre en quarantaine les copies en trop sélectionnées…", self.quarantine_duplicates)
        session = self.add_menu("Session")
        self._action(session, "Exporter la session JSON…", self.export_review_session)
        self._action(session, "Importer une session JSON…", self.import_review_session)
        tags = self.add_menu("Tags")
        edit_action = self._action(tags, "Modifier les tags de l’album sélectionné…", self.edit_tags)
        edit_action.setShortcut("Ctrl+T")
        self._action(tags, "Annuler la modification sélectionnée…", self.undo_tags)
        self._action(tags, "Afficher le journal des tags", lambda: self.tabs.setCurrentIndex(self.tags_index))
        organize = self.add_menu("Rangement")
        self._action(organize, "Prévisualiser Artiste / Album (Année)…", self.preview_organization)
        self.apply_action = self._action(organize, "Appliquer le déplacement prévisualisé…", self.apply_organization)
        self._action(organize, "Restaurer un déplacement journalisé…", self.restore_organization)
        self.apply_action.setEnabled(False)

    def album_actions(self, selected):
        actions = super().album_actions(selected)
        actions += [
            ("Enregistrer une décision…", self.record_decision, len(selected) == 2),
            ("Consulter les pochettes…", self.fetch_artworks, len(selected) in (1, 2)),
            ("Empreinte audio d’une piste…", self.start_recognition, len(selected) == 1),
            ("Alias de l’artiste…", self.resolve_alias, len(selected) == 1),
            ("Prévisualiser le rangement…", self.preview_organization, len(selected) == 1),
            ("Modifier les tags…", self.edit_tags, len(selected) == 1),
            ("Voir les albums en double", lambda: self.tabs.setCurrentIndex(self.duplicates_index), True),
        ]
        return actions

    def run_task(self, label, operation, on_result, cancellable=True):
        if self.task is not None:
            return
        for action in self.extension_actions:
            action.setEnabled(False)
        super().run_task(label, operation, on_result, cancellable=cancellable)

    @Slot()
    def task_finished(self):
        super().task_finished()
        for action in self.extension_actions:
            action.setEnabled(True)
        self.apply_action.setEnabled(self.pending_plan is not None and not self.pending_plan.blockers
                                     and not self.historical and not self.requires_rescan)
        callback, self._after_task = self._after_task, None
        if callback is not None:
            QTimer.singleShot(0, callback)

    def _fresh(self):
        if self.historical or self.requires_rescan:
            self.state.setText("Inventaire à actualiser : relancez l’analyse avant de comparer les fichiers ou d’effectuer une action.")
            return False
        return True

    def _selected(self, count=None):
        selected = self.selected_albums()
        if count is not None and len(selected) != count:
            self.state.setText(f"Sélectionnez {count} album(s) dans l’inventaire, puis réessayez.")
            self.tabs.setCurrentIndex(0)
            return []
        return selected

    def display_inventory(self, inventory, pairs=None):
        super().display_inventory(inventory, pairs)
        self.historical = False
        self.requires_rescan = False
        self.pending_plan = None
        self.jellyfin_result = None
        self.additional_findings = []
        self.imported_session = None
        self.current_decisions = []
        if self.extensions_ready:
            self.apply_action.setEnabled(False)
            self.jellyfin_model.removeRows(0, self.jellyfin_model.rowCount())
            self.set_page_count(self.jellyfin_index, None)
            self.refresh_duplicates()
            self.refresh_reviews()

    def refresh_reviews(self):
        from .review import ReviewStore, quality_findings
        self.quality_model.removeRows(0, self.quality_model.rowCount())
        findings = quality_findings(self.inventory)
        for finding in findings:
            append_row(self.quality_model, [finding.priority, finding.category, finding.album_path, finding.message])
        self.set_page_count(self.quality_index, len(findings))
        current = self.quality_filter.currentText()
        self.quality_filter.blockSignals(True)
        self.quality_filter.clear()
        self.quality_filter.addItem("Tous les constats")
        self.quality_filter.addItems(sorted({finding.category for finding in findings}))
        if self.quality_filter.findText(current) >= 0:
            self.quality_filter.setCurrentText(current)
        self.quality_filter.blockSignals(False)
        self.filter_quality()
        self.quality_table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        self.decision_model.removeRows(0, self.decision_model.rowCount())
        with ReviewStore(self.cache_dir / "review.sqlite3") as store:
            # La présence des notes se lit dans le seul cache local ; vérifier
            # leurs fichiers sources peut attendre un partage réseau lent.
            has_decisions = store.db.execute("SELECT 1 FROM review_decisions LIMIT 1").fetchone() is not None
        if has_decisions:
            if self.task is not None:
                self._after_task = self.refresh_decisions
            else:
                self.refresh_decisions()

    def refresh_decisions(self):
        if self.historical:
            return
        if self.task is not None:
            self._after_task = self.refresh_decisions
            return
        from .review import ReviewStore
        def operation(cancel, progress):
            with ReviewStore(self.cache_dir / "review.sqlite3") as store:
                return store.list(self.inventory)
        self.run_task("Actualisation des preuves associées aux décisions…", operation, self._populate_decisions,
                      cancellable=False)

    def _populate_decisions(self, decisions):
        self.current_decisions = decisions
        self.decision_model.removeRows(0, self.decision_model.rowCount())
        for decision in decisions:
            self._decision_row(decision)
        self.set_page_count(self.decisions_index, len(decisions))

    def _decision_row(self, decision, historical=False):
        data = asdict(decision) if hasattr(decision, "__dataclass_fields__") else decision
        append_row(self.decision_model, [data["decision"], data["left_path"], data["right_path"],
            data.get("note", ""), data.get("recorded_at", ""),
            "Historique importé" if historical else ("À actualiser" if data.get("stale") else "Inventaire concordant")])

    @Slot()
    def filter_quality(self, *_):
        self.quality_proxy.setFilterFixedString("" if self.quality_filter.currentIndex() <= 0 else self.quality_filter.currentText())

    def compare_selected(self):
        if self._fresh():
            super().compare_selected()

    def start_musicbrainz(self):
        if self._fresh():
            super().start_musicbrainz()

    def start_exact(self):
        if not self._fresh() or not self.inventory.albums or self.task is not None:
            return
        from .cache import Cache
        from .review import hashing_cost
        def operation(cancel, progress):
            with Cache(self.cache_dir / "inventory.sqlite3") as cache:
                return hashing_cost(self.inventory, cache)
        def done(cost):
            if getattr(cost, "stale_files", 0):
                self.show_error("Des fichiers ont changé depuis le scan. Relancez l’analyse avant le calcul exact.")
                return
            answer = QMessageBox.question(self, "Prévoir la lecture SHA-256",
                f"{cost.candidate_files} fichiers candidats de même taille.\n"
                f"Volume total : {byte_label(cost.candidate_bytes)}\n"
                f"Lecture estimée hors cache : {byte_label(cost.uncached_bytes)}\n\n"
                "Les hachages déjà validés seront réutilisés. Lancer la recherche ?")
            if answer == QMessageBox.StandardButton.Yes:
                self._after_task = lambda: super(AdvancedWindow, self).start_exact()
        self.run_task("Estimation du volume de lecture…", operation, done)

    def record_decision(self, decision=None, note=None):
        if not self._fresh() or self.task is not None:
            return
        selected = self._selected(2)
        if not selected:
            return
        if decision is None:
            decision, accepted = QInputDialog.getItem(self, "Décision personnelle", "Décision pour ces deux albums",
                ["Conserver les deux", "À revoir", "Faux positif"], 0, False)
            if not accepted:
                return
        if note is None:
            note, accepted = QInputDialog.getMultiLineText(self, "Note de revue", "Motif ou commentaire (facultatif)")
            if not accepted:
                return
        from .review import ReviewStore
        def operation(cancel, progress):
            with ReviewStore(self.cache_dir / "review.sqlite3") as store:
                store.record(*selected, decision, note)
                return store.list(self.inventory)
        def done(decisions):
            self._populate_decisions(decisions)
            self.tabs.setCurrentIndex(self.decisions_index)
            self.state.setText("Décision conservée dans le cache local avec les preuves de cet inventaire.")
        self.run_task("Enregistrement de la décision et de ses preuves…", operation, done, cancellable=False)

    def estimate_storage(self):
        if not self._fresh() or self.task is not None:
            return
        from .review import storage_summary
        def done(summary):
            self.storage_label.setText(f"Volume apparent : {byte_label(summary.apparent_bytes)} • "
                f"Volume physique connu : {byte_label(summary.known_unique_bytes)} • "
                f"Copies exactes physiquement distinctes : {byte_label(summary.verified_duplicate_bytes)}\n" +
                "\n".join(summary.reasons))
            self.tabs.setCurrentIndex(self.quality_index)
        self.run_task("Estimation de l’espace, sans compter deux fois les liens physiques…",
                      lambda cancel, progress: storage_summary(self.inventory, self.exact_groups), done)

    def start_jellyfin(self, server_url=None, api_key=None, mappings=None):
        if not self._fresh() or self.task is not None:
            return
        from .jellyfin import JellyfinClient, PathMapping
        if server_url is None:
            dialog = JellyfinDialog(self.root, self._jellyfin_config, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            server_url, api_key, mapping_rows = dialog.configuration()
            self._jellyfin_config = server_url, api_key, mapping_rows
            mappings = [PathMapping(*row) for row in mapping_rows]
        def operation(cancel, progress):
            return JellyfinClient(server_url, api_key).diagnose(self.inventory, mappings or [], cancel=cancel, progress=progress)
        def done(result):
            self.jellyfin_result = result
            self.jellyfin_model.removeRows(0, self.jellyfin_model.rowCount())
            for finding in result.findings:
                append_row(self.jellyfin_model, [finding.status, finding.server_path, finding.local_path,
                    finding.title, finding.item_id, finding.reason])
            self.set_page_count(self.jellyfin_index, len(result.findings))
            self.tabs.setCurrentIndex(self.jellyfin_index)
            self.state.setText(("Catalogue examiné. " if result.complete else "Diagnostic incomplet. ") + " | ".join(result.reasons))
        self.run_task("Consultation du catalogue Jellyfin…", operation, done)

    def start_recognition(self, api_key=None, track=None):
        if not self._fresh() or self.task is not None:
            return
        if track is None:
            selected = self._selected(1)
            if not selected:
                return
            tracks = selected[0].ordered_tracks
            labels = [f"{t.disc}.{t.number_in_disc} — {t.value('title')} — {Path(t.path).name}" for t in tracks]
            if not labels:
                return
            label, accepted = QInputDialog.getItem(self, "Empreinte audio", "Choisir une piste", labels, 0, False)
            if not accepted:
                return
            track = tracks[labels.index(label)]
        if api_key is None:
            api_key, accepted = QInputDialog.getText(self, "AcoustID (facultatif)",
                "Clé API AcoustID — laisser vide pour calculer uniquement l’empreinte locale", QLineEdit.EchoMode.Password)
            if not accepted:
                return
        from .recognition import AcoustIDClient, Fingerprinter
        def operation(cancel, progress):
            fingerprinter = Fingerprinter(self.cache_dir / "fingerprints.sqlite3")
            try:
                fingerprint = fingerprinter.fingerprint(track, cancel=cancel)
            finally:
                fingerprinter.close()
            if not api_key:
                return fingerprint, []
            client = AcoustIDClient(api_key, self.cache_dir / "acoustid.sqlite3")
            try:
                return fingerprint, client.lookup(fingerprint)
            finally:
                client.close()
        def done(result):
            fingerprint, recordings = result
            text = [f"<h2>Empreinte Chromaprint</h2><p>{escaped(track.path)}<br>Durée : {fingerprint.duration:.2f} s</p>",
                    "<p>L’empreinte identifie un signal audio ; les résultats AcoustID sont des indices sur l’enregistrement, pas sur l’édition.</p>"]
            if not api_key:
                text.append("<p>Empreinte locale calculée. Aucune recherche distante demandée.</p>")
            elif not recordings:
                text.append("<p>Aucun enregistrement trouvé par AcoustID.</p>")
            for recording in recordings:
                text.append(f"<p><a href='{escaped(recording.url)}'>{escaped(recording.title or recording.id)}</a> — "
                    f"{escaped(recording.artist)} — score {recording.score:.3f}</p>")
                self.additional_findings.append({"catégorie": "AcoustID", "dossier": track.path, "statut": "Indice d’enregistrement",
                    "proposé": recording.id, "motif": f"{recording.title} / {recording.artist} / score {recording.score}", "lien": recording.url})
            self.sources_details.setHtml("".join(text))
            self.tabs.setCurrentIndex(self.sources_index)
            self.state.setText("Empreinte calculée ; aucun identifiant d’édition n’a été déduit automatiquement.")
        self.run_task("Calcul Chromaprint puis recherche AcoustID si demandée…", operation, done, cancellable=not bool(api_key))

    def resolve_alias(self, name=None):
        if self.task is not None:
            return
        if name is None:
            selected = self._selected(1)
            if not selected:
                return
            name, accepted = QInputDialog.getText(self, "Alias MusicBrainz", "Nom d’artiste à rechercher", text=selected[0].value("albumartist"))
            if not accepted or not name.strip():
                return
        from .aliases import ArtistAliasResolver
        def operation(cancel, progress):
            client = ArtistAliasResolver(self.cache_dir / "aliases.sqlite3")
            try:
                return client.resolve(name)
            finally:
                client.close()
        def done(result):
            text = [f"<h2>{escaped(result.status)}</h2><p>{escaped(name)}</p>",
                    "<p>" + "<br>".join(escaped(reason) for reason in result.reasons) + "</p>"]
            for candidate in result.candidates:
                text.append(f"<h3><a href='{escaped(candidate.url)}'>{escaped(candidate.name)}</a></h3>"
                    f"<p>MBID : {escaped(candidate.id)}<br>{escaped(candidate.disambiguation)}<br>Alias : "
                    + escaped(", ".join(candidate.aliases)) + "</p>")
                self.additional_findings.append({"catégorie": "Alias artiste", "actuel": name, "proposé": candidate.name,
                    "statut": result.status, "motif": ", ".join(candidate.aliases), "lien": candidate.url})
            text.append("<p>Les noms d’origine sont conservés. Aucune fusion d’artistes n’est effectuée.</p>")
            self.sources_details.setHtml("".join(text))
            self.tabs.setCurrentIndex(self.sources_index)
        self.run_task("Recherche des alias et homonymes MusicBrainz…", operation, done, cancellable=False)

    def fetch_artworks(self, release_ids=None):
        if self.task is not None:
            return
        if release_ids is None:
            selected = self._selected()
            if len(selected) not in (1, 2):
                self.state.setText("Sélectionnez un ou deux albums pour consulter ou comparer leurs pochettes.")
                return
            release_ids = []
            for album in selected:
                ids = album.values("musicbrainz_albumid")
                release_id = next(iter(ids)) if len(ids) == 1 else ""
                release_id, accepted = QInputDialog.getText(self, "Pochette de l’édition",
                    f"MBID d’édition pour {album.value('album')}", text=release_id)
                if not accepted:
                    return
                release_ids.append(release_id.strip())
        from .artwork import ArtworkClient, compare_artworks
        def operation(cancel, progress):
            client = ArtworkClient(self.cache_dir / "artwork")
            try:
                return [client.fetch(release_id) for release_id in release_ids]
            finally:
                client.close()
        def done(groups):
            self.artworks = groups
            text = ["<h2>Pochettes par édition</h2><p>Une pochette identique ne prouve pas l’identité des fichiers audio ou de l’édition.</p>"]
            while self.cover_layout.count():
                item = self.cover_layout.takeAt(0)
                if item.widget():
                    item.widget().deleteLater()
            for release_id, covers in zip(release_ids, groups):
                text.append(f"<h3>Édition {escaped(release_id)}</h3>")
                if not covers:
                    text.append("<p>Aucune pochette disponible pour cette édition.</p>")
                for cover in covers:
                    text.append(f"<p><a href='{escaped(cover.source_url)}'>Source de l’image {escaped(cover.id)}</a>"
                        f" — {'Recto' if cover.front else 'Autre image'} — SHA-256 {escaped(cover.checksum)}</p>")
                    reader = QImageReader(str(cover.image_path))
                    size = reader.size()
                    if size.isValid() and size.width() * size.height() <= 40_000_000:
                        reader.setScaledSize(size.scaled(QSize(220, 220), Qt.AspectRatioMode.KeepAspectRatio))
                        image = reader.read()
                        if not image.isNull():
                            label = QLabel()
                            label.setPixmap(QPixmap.fromImage(image))
                            label.setToolTip(f"{release_id} — {cover.source_url}")
                            self.cover_layout.addWidget(label)
                    self.additional_findings.append({"catégorie": "Pochette", "dossier": release_id,
                        "actuel": cover.checksum, "motif": "Pochette par édition ; indice visuel uniquement", "lien": cover.source_url})
            if len(groups) == 2 and groups[0] and groups[1]:
                comparison = compare_artworks(groups[0][0], groups[1][0])
                text.append(f"<p><b>{escaped(comparison)}</b></p>")
            self.sources_details.setHtml("".join(text))
            self.tabs.setCurrentIndex(self.sources_index)
        self.run_task("Consultation des pochettes Cover Art Archive…", operation, done, cancellable=False)

    # Éditeur de tags : saisie, aperçu, écriture journalisée puis actualisation par scan.
    def edit_tags(self, dialog_values=None, confirm=None):
        """`dialog_values` (valeurs d'album, valeurs de pistes) et `confirm` servent aux tests."""
        if not self._fresh() or self.task is not None:
            return
        selected = self._selected(1)
        if not selected:
            return
        album = selected[0]
        from .tag_editor_gui import TagEditorDialog, TagPreviewDialog
        from .tagwriter import TagJournal, plan_album_edits
        if dialog_values is None:
            verification = self.verifications.get(album.path)
            dialog = TagEditorDialog(album, getattr(verification, "proposals", None), self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            edits = dialog.edits()
        else:
            edits = plan_album_edits(album, *dialog_values)
        if not edits:
            self.state.setText("Aucune différence avec les tags actuels : rien à écrire.")
            return
        if confirm is None:
            if TagPreviewDialog(edits, self).exec() != QDialog.DialogCode.Accepted:
                return
        elif not confirm:
            return
        def operation(cancel, progress):
            with TagJournal(self.cache_dir / "tags.sqlite3") as journal:
                return journal.apply(edits, album.path, cancel=cancel, progress=progress)
        def done(result):
            self._tags_written(result, "écrits")
        self.run_task(f"Écriture des tags de {len(edits)} fichier(s), avec vérification par relecture…",
                      operation, done, cancellable=True)

    def _tags_written(self, result, verb):
        self.refresh_tag_journal(select=result.batch_id)
        self.tabs.setCurrentIndex(self.tags_index)
        message = f"Tags {verb} pour {len(result.written)} fichier(s)."
        if result.failed:
            message += f" {len(result.failed)} échec(s) : " + " | ".join(
                f"{Path(path).name} : {error}" for path, error in result.failed)
            self.show_error(message)
        else:
            self.state.setText(message + " Actualisation de l’inventaire…")
        if result.written:
            # Seuls les fichiers modifiés sont relus : l'actualisation est rapide.
            self.requires_rescan = True
            self._after_task = self.start_scan

    def refresh_tag_journal(self, select=None):
        from .tagwriter import TagJournal
        self.tag_batch_model.removeRows(0, self.tag_batch_model.rowCount())
        with TagJournal(self.cache_dir / "tags.sqlite3") as journal:
            batches = journal.batches()
        from .gui import format_scan_date
        for batch in batches:
            state = ("Annulée le " + format_scan_date(batch.undone_at)) if batch.undone_at else (
                "Écrite" if batch.written == batch.files else f"Partielle ({batch.written}/{batch.files})")
            append_row(self.tag_batch_model, [batch.id, format_scan_date(batch.created_at), batch.album_path,
                                              f"{batch.written}/{batch.files}", state], batch.id)
        self.set_page_count(self.tags_index, len(batches))
        if select is not None:
            for row in range(self.tag_batch_proxy.rowCount()):
                if self.tag_batch_proxy.index(row, 0).data(Qt.ItemDataRole.UserRole) == select:
                    self.tag_batch_table.selectRow(row)
                    break

    def _selected_batch(self):
        rows = self.tag_batch_table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.tag_batch_proxy.index(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)

    def show_tag_batch(self, *_):
        batch_id = self._selected_batch()
        if batch_id is None:
            return
        from .tagwriter import FIELD_LABELS, TagJournal, join_values
        with TagJournal(self.cache_dir / "tags.sqlite3") as journal:
            entries = journal.entries(batch_id)
        states = {"written": "écrit", "pending": "interrompu", "failed": "échec",
                  "failed_restored": "échec, valeurs d’origine rétablies", "undone": "annulé"}
        lines = [f"<h2>Modification n° {batch_id}</h2><table border='1' cellspacing='0' cellpadding='4'>"
                 "<tr><th>Fichier</th><th>État</th><th>Champ</th><th>Avant</th><th>Après</th></tr>"]
        for path, old, new, status in entries:
            for key, values in new.items():
                lines.append("<tr>" + "".join(f"<td>{escaped(value)}</td>" for value in (
                    Path(path).name, states.get(status, status), FIELD_LABELS.get(key, key),
                    join_values(old.get(key, [])) or "—", join_values(values) or "(supprimé)")) + "</tr>")
        lines.append("</table>")
        self.tag_details.setHtml("".join(lines))

    def undo_tags(self, batch_id=None, confirm=None):
        if self.task is not None:
            return
        batch_id = batch_id if batch_id is not None else self._selected_batch()
        if batch_id is None:
            self.tabs.setCurrentIndex(self.tags_index)
            self.state.setText("Sélectionnez une modification dans le journal des tags.")
            return
        if confirm is None:
            confirm = QMessageBox.question(self, "Annuler une modification de tags",
                f"Rétablir les valeurs d’origine de la modification n° {batch_id} ?\n\n"
                "Les fichiers dont les tags ont changé depuis l’écriture ne seront pas touchés.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
        if not confirm:
            return
        from .tagwriter import TagJournal
        def operation(cancel, progress):
            with TagJournal(self.cache_dir / "tags.sqlite3") as journal:
                return journal.undo(batch_id, cancel=cancel, progress=progress)
        self.run_task("Rétablissement des tags d’origine…", operation,
                      lambda result: self._tags_written(result, "rétablis"), cancellable=True)

    # Albums en double : groupes, copie à garder, quarantaine journalisée.
    def refresh_duplicates(self):
        from .duplicates import duplicate_groups
        from .gui import STATUS_COLORS
        from .comparison import IDENTICAL, MATCHING
        from PySide6.QtGui import QColor
        self.duplicate_groups = [] if self.historical else duplicate_groups(self.matches)
        colors = {"Copie exacte": STATUS_COLORS[IDENTICAL], "Même album probable": STATUS_COLORS[MATCHING],
                  "À vérifier": "#9fb3c1"}
        self.duplicate_model.removeRows(0, self.duplicate_model.rowCount())
        for index, group in enumerate(self.duplicate_groups):
            self._duplicate_row(index, group)
            self.duplicate_model.item(index, 0).setForeground(QColor(colors.get(group.confidence, "#e3ebf1")))
        self.duplicate_table.resizeColumnsToContents()
        for column, width in ((1, 260), (3, 320), (5, 360)):
            self.duplicate_table.setColumnWidth(column, min(self.duplicate_table.columnWidth(column), width))
        self.set_page_count(self.duplicates_index, len(self.duplicate_groups))
        self.duplicate_details.setHtml("<h2>Albums en double</h2><p>" + (
            "Sélectionnez un groupe pour comparer ses copies." if self.duplicate_groups else
            "Aucun album en double détecté dans cet inventaire.") + "</p>")


    def _duplicate_row(self, index, group):
        from .gui import byte_size
        kept = Path(group.albums[group.keep].path).name if group.keep is not None else "À choisir"
        values = [group.confidence, group.title, len(group.albums), kept,
                  byte_size(group.extra_bytes) if group.keep is not None else "—", group.reasons[-1] if group.reasons else ""]
        if self.duplicate_model.rowCount() > index:
            for column, value in enumerate(values):
                self.duplicate_model.item(index, column).setText(str(value))
                self.duplicate_model.item(index, column).setToolTip(str(value))
        else:
            append_row(self.duplicate_model, values, index)

    def _selected_groups(self):
        rows = self.duplicate_table.selectionModel().selectedRows()
        return sorted({self.duplicate_proxy.index(row.row(), 0).data(Qt.ItemDataRole.UserRole) for row in rows})

    def show_duplicate_group(self, *_):
        selected = self._selected_groups()
        if not selected:
            return
        group = self.duplicate_groups[selected[0]]
        lines = [f"<h2>{escaped(group.title)} — {escaped(group.confidence)}</h2><p>"
                 + "<br>".join(escaped(reason) for reason in group.reasons) + "</p>"]
        for index, (album, score) in enumerate(zip(group.albums, group.scores)):
            label = "À garder" if index == group.keep else ("Quarantaine" if group.keep is not None else "À choisir")
            size = sum(track.size for track in album.tracks)
            lines.append(f"<h3>{escaped(label)} — {escaped(Path(album.path).name)}</h3><p>{escaped(album.path)}<br>"
                         f"{len(album.tracks)} pistes • {size / 2**20:.1f} Mio<br>"
                         f"<span style='color:#8fd1a8'>{escaped(', '.join(score.strengths))}</span><br>"
                         f"<span style='color:#ff9e80'>{escaped(', '.join(score.weaknesses))}</span></p>")
        self.duplicate_details.setHtml("".join(lines))

    def choose_kept_copy(self, group_index=None, keep=None):
        selected = self._selected_groups()
        if group_index is None:
            if len(selected) != 1:
                self.tabs.setCurrentIndex(self.duplicates_index)
                self.state.setText("Sélectionnez un seul groupe d’albums en double.")
                return
            group_index = selected[0]
        group = self.duplicate_groups[group_index]
        if keep is None:
            labels = [f"{Path(album.path).name} — {len(album.tracks)} pistes — {', '.join(score.strengths) or 'aucun atout'}"
                      for album, score in zip(group.albums, group.scores)]
            label, accepted = QInputDialog.getItem(self, "Copie à garder", "Garder cette copie ; les autres iront en "
                                                   "quarantaine", labels, group.keep or 0, False)
            if not accepted:
                return
            keep = labels.index(label)
        group.keep = keep
        group.reasons = [reason for reason in group.reasons if not reason.startswith(("Copie recommandée", "Copies de qualité",
                                                                                     "Correspondance incertaine"))]
        group.reasons.insert(0, "Copie à garder choisie manuellement.")
        self._duplicate_row(group_index, group)
        self.show_duplicate_group()

    def quarantine_duplicates(self, confirm=None, allow_hardlinks=None):
        if not self._fresh() or self.task is not None:
            return
        selected = self._selected_groups()
        if not selected:
            self.tabs.setCurrentIndex(self.duplicates_index)
            self.state.setText("Sélectionnez un ou plusieurs groupes d’albums en double (Ctrl+clic).")
            return
        undecided = [self.duplicate_groups[index].title for index in selected if self.duplicate_groups[index].keep is None]
        if undecided:
            self.state.setText("Choisissez d’abord la copie à garder pour : " + ", ".join(undecided))
            return
        albums = [album for index in selected for album in self.duplicate_groups[index].to_remove()]
        root = Path(self.root)
        from .organizer import plan_quarantine
        def operation(cancel, progress):
            plans = []
            for position, album in enumerate(albums, 1):
                plans.append(plan_quarantine(album, root, cancel=cancel))
                progress(position, len(albums), album.path)
            return plans
        self.run_task("Vérification des copies à mettre en quarantaine…", operation,
                      lambda plans: self._confirm_quarantine(plans, confirm, allow_hardlinks))

    def _confirm_quarantine(self, plans, confirm, allow_hardlinks):
        ready = [plan for plan in plans if not plan.blockers]
        blocked = [plan for plan in plans if plan.blockers]
        if blocked:
            self.duplicate_details.setHtml("<h2>Copies non déplaçables</h2>" + "".join(
                f"<p><b>{escaped(plan.source)}</b><br>{escaped(' ; '.join(plan.blockers))}</p>" for plan in blocked))
        if not ready:
            self.show_error("Aucune copie ne peut être mise en quarantaine : voir le détail des blocages.")
            return
        hardlinked = sum(plan.hardlinked_files for plan in ready)
        size = sum(entry.size for plan in ready for entry in plan.files if entry.kind == "file")
        if confirm is None:
            confirm = QMessageBox.question(self, "Mettre en quarantaine",
                f"Déplacer {len(ready)} dossier(s) ({size / 2**20:.1f} Mio) vers :\n{Path(self.root) / QUARANTINE_DIR}\n\n"
                + (f"{len(blocked)} autre(s) copie(s) bloquée(s), détail dans la page.\n\n" if blocked else "")
                + "Si qBittorrent partage encore ces fichiers, le partage sera interrompu.\n"
                "Un journal par dossier permet de tout restaurer. Rien n’est effacé : videz vous-même la "
                "quarantaine quand vous êtes sûr.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
        if not confirm:
            return
        if hardlinked and allow_hardlinks is None:
            allow_hardlinks = QMessageBox.question(self, "Liens physiques",
                f"{hardlinked} fichier(s) ont des liens physiques (Lidarr, qBittorrent…). Les déplacer ne libère pas "
                "leur espace tant que l’autre lien existe. Continuer ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
            if not allow_hardlinks:
                return
        from .organizer import OrganizeError, apply_plan
        journal_dir = self.cache_dir / "journals"
        def operation(cancel, progress):
            done, failed = [], []
            for position, plan in enumerate(ready, 1):
                try:
                    done.append(apply_plan(plan, journal_dir, confirmed=True, allow_hardlinks=bool(allow_hardlinks)))
                except (OSError, OrganizeError) as exc:
                    failed.append((plan.source, str(exc)))
                progress(position, len(ready), plan.source)
            return done, failed
        def finished(result):
            done, failed = result
            message = f"{len(done)} copie(s) mise(s) en quarantaine dans {QUARANTINE_DIR}."
            if failed:
                self.show_error(message + " Échecs : " + " | ".join(f"{Path(path).name} : {error}" for path, error in failed))
            else:
                self.state.setText(message + " Actualisation de l’inventaire…")
            if done:
                self.requires_rescan = True
                self._after_task = self.start_scan
        # Appelé depuis le résultat de la vérification : la tâche suivante démarre après sa fin.
        start = lambda: self.run_task("Mise en quarantaine des copies en trop…", operation, finished, cancellable=False)
        if self.task is not None:
            self._after_task = start
        else:
            start()

    def preview_organization(self, destination_root=None):
        if not self._fresh() or self.task is not None:
            return
        selected = self._selected(1)
        if not selected:
            return
        if destination_root is None:
            destination_root = QFileDialog.getExistingDirectory(self, "Racine de rangement (même volume)", self.root)
            if not destination_root:
                return
        from .organizer import plan_album
        def done(plan):
            self.pending_plan = plan
            text = ["<h2>Prévisualisation — aucun déplacement effectué</h2>",
                f"<p><b>Source</b> : {escaped(plan.source)}<br><b>Destination</b> : {escaped(plan.destination)}</p>",
                f"<p>{len(plan.files)} entrées contrôlées ; {plan.hardlinked_files} fichier(s) avec liens physiques.</p>",
                "<h3>Points à contrôler</h3><p>" + "<br>".join(escaped(w) for w in plan.warnings) + "</p>",
                "<h3>Blocages</h3><p>" + ("<br>".join(escaped(b) for b in plan.blockers) or "Aucun blocage détecté.") + "</p>",
                "<p>Menu Rangement → Appliquer pour demander la confirmation. Aucun tag ne sera écrit.</p>"]
            self.organizer_details.setHtml("".join(text))
            self.tabs.setCurrentIndex(self.organizer_index)
        self.run_task("Vérification du plan de rangement et des liens physiques…",
                      lambda cancel, progress: plan_album(selected[0], Path(destination_root), cancel=cancel), done)

    def apply_organization(self):
        if not self._fresh() or self.task is not None or self.pending_plan is None:
            return
        plan = self.pending_plan
        if plan.blockers:
            self.show_error("Déplacement refusé : " + " | ".join(plan.blockers))
            self.tabs.setCurrentIndex(self.organizer_index)
            return
        answer = QMessageBox.question(self, "Confirmer le déplacement",
            f"Déplacer réellement ce dossier ?\n\n{plan.source}\n→ {plan.destination}\n\n"
            "Un journal sera enregistré avant l’opération. Les tags ne seront pas modifiés.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        allow_hardlinks = False
        if plan.hardlinked_files:
            allow_hardlinks = QMessageBox.question(self, "Confirmer la présence de liens physiques",
                "Ce dossier contient des liens physiques, par exemple issus de Lidarr. Le déplacement conserve les liens "
                "sur le même volume. Confirmez-vous avoir pris en compte les autres applications qui utilisent ces chemins ?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes
            if not allow_hardlinks:
                return
        from .organizer import apply_plan
        def done(journal):
            self.pending_plan = None
            self.requires_rescan = True
            self.summary.setText("INVENTAIRE À ACTUALISER — " + self.summary.text())
            self.organizer_details.append(f"<h3>Déplacement terminé</h3><p>Journal : {escaped(journal)}</p>"
                "<p>Relancez l’analyse pour actualiser les chemins. Le menu Rangement permet de restaurer ce journal.</p>")
            self.state.setText(f"Déplacement terminé. Journal : {journal}. Relancez l’analyse.")
        self.run_task("Application du déplacement confirmé…",
            lambda cancel, progress: apply_plan(plan, self.cache_dir / "journals", confirmed=True, allow_hardlinks=allow_hardlinks),
            done, cancellable=False)

    def restore_organization(self, journal_path=None):
        if self.task is not None:
            return
        if journal_path is None:
            journal_path, _ = QFileDialog.getOpenFileName(self, "Choisir le journal à restaurer",
                str(self.cache_dir / "journals"), "Journal (*.json)")
            if not journal_path:
                return
        try:
            path = Path(journal_path)
            if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("Journal lié ou trop volumineux")
            record = json.loads(path.read_text("utf-8"))
            if not isinstance(record, dict) or record.get("version") != 1:
                raise ValueError("Version de journal inconnue")
            source, destination = record["source"], record["destination"]
            if not isinstance(source, str) or not isinstance(destination, str):
                raise ValueError("Chemins de journal invalides")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.show_error(f"Journal illisible : {exc}")
            return
        if QMessageBox.question(self, "Restaurer un déplacement", f"Restaurer ce déplacement ?\n\n"
            f"Chemin actuel : {destination}\nRetour vers : {source}\n\nJournal : {journal_path}\n\n"
            "La restauration sera refusée si le contenu a changé ou si la source est occupée.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        from .organizer import restore_journal
        def done(journal):
            self.pending_plan = None
            self.requires_rescan = True
            self.summary.setText("INVENTAIRE À ACTUALISER — " + self.summary.text())
            self.organizer_details.append(f"<h3>Restauration terminée</h3><p>{escaped(journal)}</p><p>Relancez l’analyse.</p>")
            self.tabs.setCurrentIndex(self.organizer_index)
            self.state.setText("Restauration terminée. Relancez l’analyse pour actualiser l’inventaire.")
        self.run_task("Restauration du déplacement journalisé…",
            lambda cancel, progress: restore_journal(Path(journal_path), confirmed=True), done, cancellable=False)

    def export_review_session(self, path=None):
        if self.task is not None:
            return
        if path is None:
            path, _ = QFileDialog.getSaveFileName(self, "Exporter la session de revue", "MusicScope-session.json", "JSON (*.json)")
            if not path:
                return
        from .review import ReviewStore
        from .session import export_session
        def operation(cancel, progress):
            if self.historical and self.imported_session is not None:
                export_session(Path(path), self.inventory, source_session=self.imported_session)
                return path
            with ReviewStore(self.cache_dir / "review.sqlite3") as store:
                decisions = store.list(self.inventory)
            export_session(Path(path), self.inventory, matches=self.matches,
                           exact_groups=self.exact_groups, decisions=decisions)
            return path
        def done(saved):
            self.session_details.setPlainText(f"Session exportée : {saved}\nLes preuves portent sur la date du scan. "
                                              "Les identifiants de connexion ne sont pas inclus.")
            self.tabs.setCurrentIndex(self.session_index)
            self.state.setText(f"Session enregistrée : {saved}")
        self.run_task("Export du rapport de session…", operation, done)

    def import_review_session(self, path=None, path_mappings=None):
        if self.task is not None:
            return
        if path is None:
            path, _ = QFileDialog.getOpenFileName(self, "Importer une session historique", "", "JSON (*.json)")
            if not path:
                return
            old_root, accepted = QInputDialog.getText(self, "Correspondance de chemins (facultative)",
                "Ancienne racine à remplacer — laisser vide pour conserver les chemins")
            if not accepted:
                return
            if old_root:
                new_root = QFileDialog.getExistingDirectory(self, "Nouvelle racine locale")
                if not new_root:
                    return
                path_mappings = [(old_root, new_root)]
        from .session import import_session
        def done(session):
            self.display_inventory(session.inventory)
            self.historical = True
            self.imported_session = session
            self.root = session.inventory.root
            self.root_label.setText(self.root)
            self.apply_action.setEnabled(False)
            self.decision_model.removeRows(0, self.decision_model.rowCount())
            for decision in session.decisions:
                self._decision_row(decision, historical=True)
            text = [f"<h2>Session historique — {escaped(session.exported_at)}</h2>",
                f"<p>Règles : {escaped(session.rules_version)}<br>Racine d’origine : {escaped(session.original_root)}</p>",
                "<p>Aucune preuve importée n’est considérée comme actuelle. Relancez l’analyse avant toute comparaison physique ou déplacement.</p>"]
            for comparison in session.comparisons:
                text.append(f"<h3>{escaped(comparison['status'])} — historique</h3><p>{escaped(comparison['left_path'])}<br>"
                    f"{escaped(comparison['right_path'])}<br>" + "<br>".join(escaped(reason) for reason in
                    comparison.get("reasons", []) + comparison.get("differences", [])) + "</p>")
            text.append(f"<p>{len(session.exact_groups)} groupe(s) de copies exactes historiques ; {len(session.decisions)} décision(s) importée(s).</p>")
            self.session_details.setHtml("".join(text))
            self.summary.setText("SESSION HISTORIQUE — " + self.summary.text())
            self.tabs.setCurrentIndex(self.session_index)
            self.state.setText("Session importée. Preuves historiques : relancez l’analyse pour les actualiser.")
        self.run_task("Lecture de la session et des correspondances de chemins…",
                      lambda cancel, progress: import_session(Path(path), path_mappings=path_mappings), done)

    def findings(self):
        rows = super().findings()
        rows.extend(self.additional_findings)
        if self.jellyfin_result is not None:
            for finding in self.jellyfin_result.findings:
                rows.append({"catégorie": "Jellyfin", "dossier": finding.local_path,
                    "autre_dossier": finding.server_path, "statut": finding.status,
                    "actuel": finding.item_id, "motif": finding.reason})
        for decision in self.current_decisions:
            rows.append({"catégorie": "Décision", "dossier": decision.left_path, "autre_dossier": decision.right_path,
                "statut": decision.decision, "motif": decision.note + (" — À actualiser" if decision.stale else "")})
        if self.historical and self.imported_session is not None:
            for decision in self.imported_session.decisions:
                rows.append({"catégorie": "Décision", "dossier": decision["left_path"], "autre_dossier": decision["right_path"],
                    "statut": decision["decision"], "motif": decision.get("note", "")})
        if self.historical or self.requires_rescan:
            for row in rows:
                row["statut"] = ("Historique — " if self.historical else "À actualiser — ") + row.get("statut", "")
        return rows
