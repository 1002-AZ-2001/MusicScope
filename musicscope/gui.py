"""Interface française ; toutes les opérations longues tournent hors du fil Qt."""
from __future__ import annotations

import html
import json
import platform
import sqlite3
import threading
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as package_version
from importlib.resources import files
from pathlib import Path

from PySide6.QtCore import QByteArray, QSortFilterProxyModel, Qt, QThread, QUrl, Signal, Slot, qVersion
from PySide6.QtGui import (
    QAction, QColor, QDesktopServices, QIcon, QKeySequence, QPalette, QPixmap, QShortcut, QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox, QProgressBar,
    QPushButton, QSizePolicy, QSplitter, QStyle, QStyledItemDelegate, QTabWidget, QTableView, QTextBrowser,
    QVBoxLayout, QWidget,
)

from . import __version__

from .audit import audit_album
from .cache import Cache
from .comparison import (
    DISTINCT, IDENTICAL, MATCHING, PHYSICAL, PROBABLE, candidates, compare_albums, exact_file_groups,
)
from .export import export_csv
from .models import Inventory
from .musicbrainz import MusicBrainzClient
from .paths import check_cache_outside_library
from .scanner import ScanCancelled, scan


REPOSITORY_URL = "https://github.com/1002-AZ-2001/MusicScope"
DOCUMENTATION_URL = f"{REPOSITORY_URL}#readme"
ISSUES_URL = f"{REPOSITORY_URL}/issues"


# Palette unique de l'interface : fond, surfaces, texte et accent.
COLORS = {
    "window": "#17212b", "surface": "#1d2a35", "surface_alt": "#22323e", "raised": "#243744",
    "border": "#3a4d5a", "text": "#e3ebf1", "muted": "#9fb3c1", "accent": "#147d88",
    "accent_text": "#80cbc4", "button": "#284453", "button_hover": "#346273",
}
SECTIONS = ("Bibliothèque", "Doublons", "Services", "Organisation")
# Statuts de rapprochement : les copies certaines ressortent, les éditions distinctes s'effacent.
STATUS_COLORS = {PHYSICAL: "#ff9e80", IDENTICAL: "#ff9e80", PROBABLE: "#ffd27f", MATCHING: "#fff1a8",
                 DISTINCT: "#8fd1a8"}
BROWSER_CSS = f"""
    body {{ color:{COLORS['text']}; }}
    h2 {{ color:#ffffff; }} h3 {{ color:{COLORS['accent_text']}; }}
    a {{ color:{COLORS['accent_text']}; }}
    table {{ border-color:{COLORS['border']}; }}
    th {{ background-color:{COLORS['raised']}; color:#ffffff; }}
"""
STYLE_SHEET = f"""
    QMainWindow, QDialog {{background:{COLORS['window']};color:{COLORS['text']}}}
    QWidget {{font-size:13px}}
    QLabel {{color:{COLORS['text']}}}
    QLabel#title {{font-size:19px;font-weight:700}}
    QLabel#summary {{color:{COLORS['muted']}}}
    QPushButton {{padding:7px 12px;background:{COLORS['button']};color:#f4f8fb;
        border:1px solid #426371;border-radius:5px}}
    QPushButton:hover {{background:{COLORS['button_hover']}}}
    QPushButton#primary {{background:{COLORS['accent']};border-color:#1f9aa6;font-weight:600}}
    QPushButton#primary:hover {{background:#1a929e}}
    QPushButton:disabled {{color:#82939e;background:#24313b;border-color:#33414b}}
    QLineEdit {{padding:7px;border:1px solid #445764;border-radius:5px;background:#202f3a;color:#eef4fa}}
    QComboBox {{padding:5px;background:#202f3a;color:#eef4fa;border:1px solid #445764;border-radius:5px}}
    QTableView, QTextBrowser, QListWidget, QTableWidget {{background:{COLORS['surface']};
        alternate-background-color:{COLORS['surface_alt']};color:{COLORS['text']};border:1px solid {COLORS['border']};
        selection-background-color:{COLORS['accent']};selection-color:white;gridline-color:{COLORS['border']}}}
    QHeaderView::section {{padding:7px;background:{COLORS['raised']};color:#cfe0ea;border:0;
        border-right:1px solid {COLORS['border']};font-weight:600}}
    QListWidget#navigation {{background:{COLORS['window']};border:0;border-right:1px solid {COLORS['border']};
        padding:6px 0;outline:0}}
    QListWidget#navigation::item {{padding:8px 14px;border-radius:5px;margin:1px 6px}}
    QListWidget#navigation::item:selected {{background:{COLORS['accent']};color:white}}
    QListWidget#navigation::item:hover:!selected {{background:{COLORS['raised']}}}
    QTabWidget::pane {{border:0}}
    QSplitter::handle {{background:{COLORS['border']}}}
    QStatusBar {{background:{COLORS['raised']};color:{COLORS['text']}}}
    QStatusBar QLabel {{padding:0 6px}}
    QProgressBar {{background:{COLORS['surface']};color:white;border:0;border-radius:4px;text-align:center;
        max-height:16px}}
    QProgressBar::chunk {{background:#239ba2;border-radius:4px}}
    QMenuBar, QMenu {{background:{COLORS['raised']};color:{COLORS['text']}}}
    QMenuBar::item:selected, QMenu::item:selected {{background:{COLORS['accent']}}}
    QMenu::item:disabled {{color:#6f818d}}
    QToolTip {{background:{COLORS['raised']};color:{COLORS['text']};border:1px solid {COLORS['border']}}}
"""


def escaped(value) -> str:
    return html.escape(str(value))


def install_french_translation(app) -> bool:
    """Boutons standard de Qt (Oui, Non, Annuler…) en français, si la traduction est fournie."""
    from PySide6.QtCore import QLibraryInfo, QTranslator
    translator = QTranslator(app)
    if translator.load("qtbase_fr", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(translator)
        app._musicscope_translator = translator  # garder une référence Python
        return True
    return False


def dark_palette() -> QPalette:
    """Palette assortie à la feuille de style, pour les éléments natifs (barres de défilement…)."""
    palette = QPalette()
    for role, color in ((QPalette.ColorRole.Window, COLORS["window"]), (QPalette.ColorRole.WindowText, COLORS["text"]),
                        (QPalette.ColorRole.Base, COLORS["surface"]), (QPalette.ColorRole.AlternateBase, COLORS["surface_alt"]),
                        (QPalette.ColorRole.Text, COLORS["text"]), (QPalette.ColorRole.Button, COLORS["button"]),
                        (QPalette.ColorRole.ButtonText, "#f4f8fb"), (QPalette.ColorRole.Highlight, COLORS["accent"]),
                        (QPalette.ColorRole.HighlightedText, "#ffffff"), (QPalette.ColorRole.Link, COLORS["accent_text"]),
                        (QPalette.ColorRole.ToolTipBase, COLORS["raised"]), (QPalette.ColorRole.ToolTipText, COLORS["text"]),
                        (QPalette.ColorRole.PlaceholderText, COLORS["muted"])):
        palette.setColor(role, QColor(color))
    return palette


def make_browser(external_links: bool = False) -> QTextBrowser:
    """Panneau de détail au thème sombre, liens et tableaux lisibles."""
    browser = QTextBrowser()
    browser.document().setDefaultStyleSheet(BROWSER_CSS)
    browser.setOpenExternalLinks(external_links)
    return browser


def format_scan_date(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%d/%m/%Y à %H:%M")
    except (TypeError, ValueError):
        return "date inconnue"


class StatusLabel(QLabel):
    """Message d'état sur une ligne ; le texte complet reste dans l'infobulle."""

    def __init__(self, text=""):
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text):  # noqa: N802 - API Qt
        super().setText(text)
        self.setToolTip(text)


class RelativePathDelegate(QStyledItemDelegate):
    """Affiche les chemins relativement à la racine ; le modèle garde le chemin complet."""

    def __init__(self, window, parent=None):
        super().__init__(parent)
        self.window = window

    def displayText(self, value, locale):  # noqa: N802 - API Qt
        text = str(value)
        root = getattr(self.window, "root", "")
        if root and text.startswith(root.rstrip("/\\") + ("\\" if "\\" in root else "/")):
            relative = text[len(root.rstrip("/\\")) + 1:]
            return relative or text
        return text


def app_icon() -> QIcon:
    """Icône PNG embarquée : aucun module d'image SVG requis dans le binaire."""
    pixmap = QPixmap()
    pixmap.loadFromData(files("musicscope").joinpath("data/musicscope.png").read_bytes(), "PNG")
    return QIcon(pixmap)


def _dependency_version(name: str) -> str:
    try:
        return package_version(name)
    except PackageNotFoundError:
        return "inconnue"


def about_html(cache_dir: Path) -> str:
    """Version, licence, composants et état du cache, sans accès réseau."""
    try:
        with Cache(cache_dir / "inventory.sqlite3") as cache:
            stats = cache.stats()
        cache_text = (f"{stats['tracks']} fichiers aux tags mémorisés, {stats['hashes']} SHA-256, "
                      f"{stats['inventories']} inventaire(s) ; {stats['bytes'] / 2**20:.1f} Mio")
    except (OSError, ValueError, sqlite3.Error) as exc:
        # L'état du cache est une information facultative de la fenêtre.
        cache_text = f"indisponible ({exc})"
    rows = [
        ("Version", __version__),
        ("Licence", "GNU AGPLv3"),
        ("Dépôt", f"<a href='{REPOSITORY_URL}'>{REPOSITORY_URL}</a>"),
        ("Signaler un problème", f"<a href='{ISSUES_URL}'>{ISSUES_URL}</a>"),
        ("Python", escaped(platform.python_version())),
        ("Qt / PySide6", f"{escaped(qVersion())} / {escaped(_dependency_version('PySide6'))}"),
        ("Mutagen", escaped(_dependency_version("mutagen"))),
        ("Système", escaped(platform.platform())),
        ("Cache", f"{escaped(cache_dir)}<br>{escaped(cache_text)}"),
    ]
    table = "".join(f"<tr><td style='padding-right:14px;color:#80cbc4'>{name}</td><td>{value}</td></tr>"
                    for name, value in rows)
    return (f"<h2>MusicScope {escaped(__version__)}</h2>"
            "<p>Audit musical en lecture seule : inventaire, ressemblances entre albums, "
            "vérification des métadonnées et rangement confirmé.</p>"
            "<p>Les analyses réutilisent le cache local : seuls les fichiers modifiés sont relus. "
            "Sur un partage réseau, les accès disque sont parallélisés.</p>"
            f"<table>{table}</table>")


class AboutDialog(QDialog):
    def __init__(self, cache_dir: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("À propos de MusicScope")
        self.setWindowIcon(app_icon())
        self.resize(620, 470)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(72, 72))
        header.addWidget(logo, 0, Qt.AlignmentFlag.AlignTop)
        self.text = make_browser(external_links=True)
        self.text.setHtml(about_html(cache_dir))
        header.addWidget(self.text, 1)
        layout.addLayout(header)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).setText("Fermer")
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


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
        self.pages: list[tuple[str, str, int]] = []
        self.page_counts: dict[int, int] = {}
        self.setWindowTitle("MusicScope — Audit de votre médiathèque")
        self.setWindowIcon(app_icon())
        self.resize(1320, 860)
        self.file_menu = self.menuBar().addMenu("Fichier")
        self.view_menu = self.menuBar().addMenu("Affichage")
        self.help_menu = self._build_help_menu()
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(14, 10, 14, 6)
        layout.setSpacing(8)

        header = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(app_icon().pixmap(30, 30))
        header.addWidget(logo)
        title = QLabel("MusicScope")
        title.setObjectName("title")
        header.addWidget(title)
        header.addSpacing(10)
        self.root_label = QLineEdit()
        self.root_label.setPlaceholderText("Choisissez votre dossier musical local ou réseau…")
        self.root_label.setReadOnly(True)
        header.addWidget(self.root_label, 1)
        self.choose_button = self._button("Choisir le dossier", QStyle.StandardPixmap.SP_DirOpenIcon,
                                          self.choose_root, "Choisir la racine de la médiathèque (Ctrl+O)")
        self.scan_button = self._button("Analyser", QStyle.StandardPixmap.SP_BrowserReload, self.start_scan,
                                        "Analyser ou actualiser : seuls les fichiers modifiés sont relus (F5)")
        self.scan_button.setObjectName("primary")
        header.addWidget(self.choose_button)
        header.addWidget(self.scan_button)
        layout.addLayout(header)

        controls = QHBoxLayout()
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filtrer par artiste, album, chemin ou constat…  (Ctrl+F)")
        self.filter.setClearButtonEnabled(True)
        controls.addWidget(self.filter, 1)
        self.open_button = self._button("Ouvrir le dossier", QStyle.StandardPixmap.SP_DirIcon, self.open_folder,
                                        "Ouvrir le dossier de l’album sélectionné")
        self.compare_button = self._button("Comparer 2 albums", QStyle.StandardPixmap.SP_FileDialogContentsView,
                                           self.compare_selected, "Comparer les deux albums sélectionnés (Ctrl+clic)")
        self.exact_button = self._button("Recherche exacte", QStyle.StandardPixmap.SP_FileDialogDetailedView,
                                         self.start_exact, "Trouver les fichiers identiques octet pour octet (SHA-256)")
        self.mb_button = self._button("MusicBrainz", QStyle.StandardPixmap.SP_DriveNetIcon, self.start_musicbrainz,
                                      "Vérifier l’album sélectionné avec MusicBrainz (aucun tag écrit)")
        self.export_button = self._button("Exporter CSV", QStyle.StandardPixmap.SP_DialogSaveButton, self.export,
                                          "Exporter l’inventaire et les constats (Ctrl+E)")
        for button in (self.open_button, self.compare_button, self.exact_button, self.mb_button, self.export_button):
            controls.addWidget(button)
        layout.addLayout(controls)
        self.summary = QLabel("Aucun inventaire. La médiathèque est ouverte en lecture seule.")
        self.summary.setObjectName("summary")
        layout.addWidget(self.summary)

        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.setFixedWidth(200)
        self.navigation.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.tabs = QTabWidget()
        self.tabs.tabBar().hide()
        self.tabs.setDocumentMode(True)
        main_row = QHBoxLayout()
        main_row.setSpacing(10)
        main_row.addWidget(self.navigation)
        main_row.addWidget(self.tabs, 1)
        layout.addLayout(main_row, 1)

        self.album_table, self.album_model, self.album_proxy = make_table(
            ["Artiste d’album", "Album", "Pistes", "Codecs", "Édition", "Constats", "Dossier"])
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self.album_table)
        self.album_details = make_browser()
        splitter.addWidget(self.album_details)
        splitter.setSizes([380, 220])
        self.add_page(splitter, "Inventaire", "Bibliothèque")
        self.match_table, self.match_model, self.match_proxy = make_table(
            ["Statut", "Album A", "Album B", "Motifs"])
        comparison_splitter = QSplitter(Qt.Orientation.Vertical)
        comparison_splitter.addWidget(self.match_table)
        detail_splitter = QSplitter()
        self.left_details = make_browser()
        self.right_details = make_browser()
        detail_splitter.addWidget(self.left_details)
        detail_splitter.addWidget(self.right_details)
        comparison_splitter.addWidget(detail_splitter)
        comparison_splitter.setSizes([350, 230])
        self.add_page(comparison_splitter, "Rapprochements", "Doublons")
        self.exact_details = make_browser()
        self.add_page(self.exact_details, "Fichiers identiques", "Doublons")
        self.mb_details = make_browser(external_links=True)
        self.add_page(self.mb_details, "MusicBrainz", "Services")
        self.log_details = make_browser()
        self.add_page(self.log_details, "Erreurs de lecture", "Bibliothèque")
        self.setCentralWidget(body)

        self.state = StatusLabel("Prêt — aucun fichier musical ne sera modifié.")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFixedWidth(220)
        self.cancel_button = self._button("Annuler", QStyle.StandardPixmap.SP_DialogCancelButton, self.cancel_task,
                                          "Annuler l’opération en cours (Échap)")
        self.cancel_button.setEnabled(False)
        self.statusBar().addWidget(self.state, 1)
        self.statusBar().addPermanentWidget(self.progress_bar)
        self.statusBar().addPermanentWidget(self.cancel_button)
        self.statusBar().setSizeGripEnabled(True)

        path_delegate = RelativePathDelegate(self, self)
        self.album_table.setItemDelegateForColumn(6, path_delegate)
        self.match_table.setItemDelegateForColumn(1, path_delegate)
        self.match_table.setItemDelegateForColumn(2, path_delegate)
        self.path_delegate = path_delegate
        self.filter.textChanged.connect(self.album_proxy.setFilterFixedString)
        self.filter.textChanged.connect(self.match_proxy.setFilterFixedString)
        self.album_table.selectionModel().selectionChanged.connect(self.show_album)
        self.match_table.selectionModel().selectionChanged.connect(self.show_match)
        self.album_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.album_table.customContextMenuRequested.connect(self.album_context_menu)
        self.album_table.doubleClicked.connect(lambda *_: self.open_folder())
        self.navigation.currentRowChanged.connect(self._navigation_changed)
        self.tabs.currentChanged.connect(self._tab_changed)
        self.buttons = [self.choose_button, self.scan_button, self.open_button, self.compare_button,
                        self.exact_button, self.mb_button, self.export_button]
        self._build_file_menu()
        QShortcut(QKeySequence.StandardKey.Find, self, activated=self.focus_filter)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self.cancel_task)
        self.setStyleSheet(STYLE_SHEET)
        self.album_details.setHtml("<h2>Votre bibliothèque, sans modification</h2><p>Choisissez une racine, puis lancez "
                                   "l’analyse. Sélectionnez deux lignes avec Ctrl pour comparer deux dossiers ; "
                                   "un clic droit sur un album propose les actions disponibles, un double-clic ouvre "
                                   "son dossier.</p>")
        self.exact_details.setHtml("<h2>Recherche exacte à la demande</h2><p>Les fichiers sont préfiltrés par taille, "
                                   "puis lus en flux pour calculer leur SHA-256, indépendamment des noms.</p>")
        self.mb_details.setHtml("<h2>Vérification MusicBrainz</h2><p>Sélectionnez un album dans l’inventaire, puis cliquez sur "
                                "« MusicBrainz ». Aucun tag n’est écrit.</p>")
        self.load_preferences()
        self.tabs.setCurrentIndex(0)
        self._tab_changed(0)

    def _button(self, text, icon, callback, tooltip) -> QPushButton:
        button = QPushButton(self.style().standardIcon(icon), text)
        button.setToolTip(tooltip)
        button.clicked.connect(lambda checked=False: callback())
        return button

    # Navigation latérale : sections fixes, pages ajoutées au fil de la construction.
    def add_page(self, widget, title: str, section: str) -> int:
        index = self.tabs.addTab(widget, title)
        self.pages.append((section, title, index))
        self._rebuild_navigation()
        return index

    def _page_label(self, title: str, index: int) -> str:
        count = self.page_counts.get(index)
        return f"{title}   {count}" if count else title

    def _rebuild_navigation(self):
        current = self.tabs.currentIndex()
        self.navigation.blockSignals(True)
        self.navigation.clear()
        shortcut = 1
        for section in SECTIONS:
            pages = [page for page in self.pages if page[0] == section]
            if not pages:
                continue
            header = QListWidgetItem(section.upper())
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            header.setForeground(QColor(COLORS["muted"]))
            font = header.font()
            font.setPointSizeF(max(7.0, font.pointSizeF() - 1.5))
            font.setBold(True)
            header.setFont(font)
            self.navigation.addItem(header)
            for _, title, index in pages:
                item = QListWidgetItem(self._page_label(title, index))
                item.setData(Qt.ItemDataRole.UserRole, index)
                item.setToolTip(f"{title} (Ctrl+{shortcut})" if shortcut <= 9 else title)
                shortcut += 1
                self.navigation.addItem(item)
        self.navigation.blockSignals(False)
        self._tab_changed(current)
        self._rebuild_view_menu()

    def set_page_count(self, index: int, count: int | None):
        if count:
            self.page_counts[index] = count
        else:
            self.page_counts.pop(index, None)
        for row in range(self.navigation.count()):
            item = self.navigation.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == index:
                title = next(title for _, title, page in self.pages if page == index)
                item.setText(self._page_label(title, index))

    def _navigation_changed(self, row: int):
        item = self.navigation.item(row)
        if item is not None and item.data(Qt.ItemDataRole.UserRole) is not None:
            self.tabs.setCurrentIndex(item.data(Qt.ItemDataRole.UserRole))

    def _tab_changed(self, index: int):
        for row in range(self.navigation.count()):
            if self.navigation.item(row).data(Qt.ItemDataRole.UserRole) == index:
                if self.navigation.currentRow() != row:
                    self.navigation.blockSignals(True)
                    self.navigation.setCurrentRow(row)
                    self.navigation.blockSignals(False)
                return

    def _build_file_menu(self):
        entries = [
            ("Choisir le dossier…", self.choose_root, QKeySequence.StandardKey.Open),
            ("Analyser", self.start_scan, QKeySequence(Qt.Key.Key_F5)),
            ("Exporter CSV…", self.export, QKeySequence("Ctrl+E")),
            (None, None, None),
            ("Quitter", self.close, QKeySequence.StandardKey.Quit),
        ]
        for title, callback, shortcut in entries:
            if title is None:
                self.file_menu.addSeparator()
                continue
            action = QAction(title, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(lambda checked=False, callback=callback: callback())
            self.file_menu.addAction(action)

    def _rebuild_view_menu(self):
        self.view_menu.clear()
        number = 1
        for section in SECTIONS:
            for _, title, index in (page for page in self.pages if page[0] == section):
                action = QAction(f"{section} › {title}", self)
                if number <= 9:
                    action.setShortcut(QKeySequence(f"Ctrl+{number}"))
                action.triggered.connect(lambda checked=False, index=index: self.tabs.setCurrentIndex(index))
                self.view_menu.addAction(action)
                number += 1

    def focus_filter(self):
        self.filter.setFocus()
        self.filter.selectAll()

    def album_context_menu(self, position):
        index = self.album_table.indexAt(position)
        if index.isValid() and not self.album_table.selectionModel().isRowSelected(index.row(), index.parent()):
            self.album_table.selectRow(index.row())
        selected = self.selected_albums()
        if not selected:
            return
        menu = QMenu(self)
        for title, callback, enabled in self.album_actions(selected):
            action = menu.addAction(title)
            action.setEnabled(enabled and self.task is None)
            action.triggered.connect(lambda checked=False, callback=callback: callback())
        menu.exec(self.album_table.viewport().mapToGlobal(position))

    def album_actions(self, selected):
        """Actions du clic droit sur l'inventaire ; étendues par la fenêtre avancée."""
        return [
            ("Ouvrir le dossier", self.open_folder, len(selected) == 1),
            ("Copier le chemin", lambda: QApplication.clipboard().setText(
                "\n".join(album.path for album in selected)), True),
            ("Comparer ces deux albums", self.compare_selected, len(selected) == 2),
            ("Vérifier avec MusicBrainz", self.start_musicbrainz, len(selected) == 1),
        ]

    def _build_help_menu(self) -> QMenu:
        menu = self.menuBar().addMenu("Aide")
        actions = [
            ("Documentation en ligne", lambda: QDesktopServices.openUrl(QUrl(DOCUMENTATION_URL)), None),
            ("Signaler un problème…", lambda: QDesktopServices.openUrl(QUrl(ISSUES_URL)), None),
            ("Ouvrir le dossier du cache", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.cache_dir))), None),
            (None, None, None),
            ("À propos de MusicScope", self.show_about, QKeySequence(QKeySequence.StandardKey.HelpContents)),
            ("À propos de Qt", lambda: QMessageBox.aboutQt(self), None),
        ]
        for title, callback, shortcut in actions:
            if title is None:
                menu.addSeparator()
                continue
            action = QAction(title, self)
            action.triggered.connect(lambda checked=False, callback=callback: callback())
            if shortcut is not None:
                action.setShortcut(shortcut)
            menu.addAction(action)
        return menu

    def add_menu(self, title: str) -> QMenu:
        """Menu inséré avant « Aide », qui reste le dernier de la barre."""
        menu = QMenu(title, self)
        self.menuBar().insertMenu(self.help_menu.menuAction(), menu)
        return menu

    def show_about(self):
        AboutDialog(self.cache_dir, self).exec()

    def _preferences(self) -> dict:
        try:
            data = json.loads((self.cache_dir / "preferences.json").read_text("utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def save_preferences(self, **values):
        """Fusionne les préférences ; une valeur None retire la clé."""
        data = self._preferences()
        data.update(values)
        data = {key: value for key, value in data.items() if value is not None}
        try:
            (self.cache_dir / "preferences.json").write_text(json.dumps(data), "utf-8")
        except OSError as exc:
            self.state.setText(f"Préférences non enregistrées : {exc}")

    def load_preferences(self):
        geometry = self._preferences().get("geometry")
        if isinstance(geometry, str):
            self.restoreGeometry(QByteArray.fromBase64(geometry.encode("ascii")))
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
                when = format_scan_date(saved.scanned_at)
                self.state.setText(f"Inventaire du {when} chargé depuis le cache ({len(saved.albums)} albums), "
                                   "sans relire les fichiers. Relancez l’analyse pour actualiser : "
                                   "seuls les fichiers modifiés seront relus.")
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
        self.save_preferences(root=self.root)
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
        self.set_page_count(0, len(inventory.albums))
        self.set_page_count(1, len(self.matches))
        self.set_page_count(2, None)
        self.set_page_count(4, len(inventory.errors))
        self.exact_details.setPlainText("Recherche exacte non exécutée pour cet inventaire.")
        self.mb_details.setPlainText("Vérification en ligne non exécutée pour cet inventaire.")
        self.summary.setText(f"{len(inventory.albums)} albums • {sum(len(a.tracks) for a in inventory.albums)} pistes • "
                             f"{len(self.matches)} rapprochements • {len(inventory.errors)} erreurs — "
                             f"analysé le {format_scan_date(inventory.scanned_at)}")

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
            color = STATUS_COLORS.get(comparison.status)
            if color:
                self.match_model.item(self.match_model.rowCount() - 1, 0).setForeground(QColor(color))
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
            self.set_page_count(1, len(self.matches))
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
            self.set_page_count(2, len(groups))
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
        self.save_preferences(geometry=bytes(self.saveGeometry().toBase64()).decode("ascii"))
        event.accept()
