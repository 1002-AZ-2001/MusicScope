"""Mise à jour depuis l'interface : contrôle au démarrage, menu Aide, installation, redémarrage."""
from __future__ import annotations

import time

from PySide6.QtCore import QProcess, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox

from . import __version__
from .updater import (
    Installation,
    Release,
    UpdateError,
    detect_installation,
    download,
    due,
    fetch_latest,
    install,
)


class CheckThread(QThread):
    found = Signal(object)
    failed = Signal(str)

    def __init__(self, fetch, parent=None):
        super().__init__(parent)
        self.fetch = fetch

    def run(self):
        try:
            self.found.emit(self.fetch())
        except UpdateError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 — un contrôle raté ne doit jamais gêner l'application
            self.failed.emit(f"{type(exc).__name__} : {exc}")


class UpdateController:
    """Rattaché à la fenêtre principale. `fetch`, `installation`, `opener`, `runner` et
    `launcher` sont remplaçables pour les tests ; par défaut : GitHub, système réel, QProcess."""

    def __init__(self, window, fetch=None, installation: Installation | None = None, opener=None, runner=None,
                 launcher=None, ask=None):
        self.window = window
        self.fetch = fetch or (lambda: fetch_latest(opener))
        self.installation = installation or detect_installation()
        self.opener, self.runner = opener, runner
        self.launcher = launcher or (lambda program, arguments: QProcess.startDetached(program, arguments)[0])
        self.ask = ask  # réponse imposée aux dialogues (tests) : "install", "page", "later", "skip"
        self.thread = None
        self.last_message = ""
        self.auto_action = None

    # Préférences
    @property
    def automatic(self) -> bool:
        return self.window._preferences().get("update_check", True) is not False

    def set_automatic(self, enabled: bool):
        self.window.save_preferences(update_check=bool(enabled))

    def install_menu(self, menu):
        first = menu.actions()[0] if menu.actions() else None
        check = QAction("Rechercher une mise à jour…", self.window)
        check.triggered.connect(lambda checked=False: self.check(silent=False))
        self.auto_action = QAction("Rechercher automatiquement chaque jour", self.window)
        self.auto_action.setCheckable(True)
        self.auto_action.setChecked(self.automatic)
        self.auto_action.toggled.connect(self.set_automatic)
        for action in (check, self.auto_action):
            menu.insertAction(first, action)
        menu.insertSeparator(first)

    # Contrôle
    def check_on_startup(self) -> bool:
        if not self.automatic or not due(self.window._preferences().get("update_last_check")):
            return False
        self.check(silent=True)
        return True

    def check(self, silent: bool):
        if self.thread is not None:
            return
        if not silent:
            self.window.state.setText("Recherche d’une mise à jour…")
        thread = CheckThread(self.fetch, self.window)
        self.thread = thread
        # File d'attente explicite : sans objet récepteur, le rappel s'exécuterait dans le thread
        # de travail, où aucune fenêtre ne doit être ouverte.
        queued = Qt.ConnectionType.QueuedConnection
        thread.found.connect(lambda release: self._found(release, silent), queued)
        thread.failed.connect(lambda message: self._failed(message, silent), queued)
        thread.finished.connect(self._finished, queued)
        thread.start()

    def _finished(self):
        if self.thread is not None:
            self.thread.deleteLater()
        self.thread = None

    def _failed(self, message: str, silent: bool):
        self.last_message = f"Mise à jour non vérifiée : {message}"
        if silent:  # hors ligne au démarrage : simple mention, sans fenêtre
            self.window.state.setText(self.last_message)
        else:
            QMessageBox.warning(self.window, "Mise à jour", self.last_message)

    def _found(self, release: Release, silent: bool):
        self.window.save_preferences(update_last_check=time.time())
        if not release.newer:
            self.last_message = f"MusicScope {__version__} est à jour."
            self.window.state.setText(self.last_message)
            if not silent:
                QMessageBox.information(self.window, "Mise à jour", self.last_message)
            return
        if silent and self.window._preferences().get("update_skipped") == release.version:
            self.last_message = f"MusicScope {release.version} disponible (version ignorée)."
            self.window.state.setText(self.last_message)
            return
        self.offer(release)

    def offer(self, release: Release):
        self.last_message = f"MusicScope {release.version} est disponible (vous avez {__version__})."
        choice = self.ask or self._dialog(release)
        if choice == "install":
            self.start_install(release)
        elif choice == "page":
            QDesktopServices.openUrl(QUrl(release.page))
        elif choice == "skip":
            self.window.save_preferences(update_skipped=release.version)
            self.window.state.setText(f"Version {release.version} ignorée ; Aide → Rechercher une mise à jour.")
        else:
            self.window.state.setText(self.last_message + " Aide → Rechercher une mise à jour.")

    def _dialog(self, release: Release) -> str:
        box = QMessageBox(self.window)
        box.setWindowTitle("Mise à jour disponible")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(self.last_message)
        details = [self.installation.detail]
        if self.installation.can_install:
            details.append("Le fichier est vérifié (origine, taille, SHA-256) avant l’installation, puis "
                           "MusicScope redémarre. Les opérations en cours doivent être terminées.")
        box.setInformativeText("\n\n".join(details))
        buttons = {}
        if self.installation.can_install:
            buttons[box.addButton("Installer et redémarrer", QMessageBox.ButtonRole.AcceptRole)] = "install"
        buttons[box.addButton("Voir les nouveautés", QMessageBox.ButtonRole.HelpRole)] = "page"
        buttons[box.addButton("Ignorer cette version", QMessageBox.ButtonRole.DestructiveRole)] = "skip"
        later = box.addButton("Plus tard", QMessageBox.ButtonRole.RejectRole)
        buttons[later] = "later"
        box.setDefaultButton(later)
        box.exec()
        return buttons.get(box.clickedButton(), "later")

    # Installation
    def start_install(self, release: Release):
        window = self.window
        if window.task is not None:
            window.state.setText("Une opération est en cours : relancez la mise à jour à sa fin (menu Aide).")
            return
        installation, opener, runner = self.installation, self.opener, self.runner
        directory = window.cache_dir / "updates"

        def operation(cancel, progress):
            package = download(release, installation, directory, opener, cancel, progress)
            progress(0, 0, "Installation de la mise à jour (autorisation administrateur demandée)…")
            result = install(installation, package, runner) if runner else install(installation, package)
            package.unlink(missing_ok=True)
            return result

        def done(result):
            self.last_message = f"MusicScope {release.version} : {result.message}"
            window.state.setText(self.last_message)

            def finish():  # après la fin de la tâche : la fenêtre peut alors se fermer
                if self.ask is None:
                    QMessageBox.information(window, "Mise à jour installée",
                                            self.last_message + "\n\nMusicScope va redémarrer.")
                self.restart(result.restart)
            window._start_after_task(finish)

        window.run_task(f"Mise à jour vers {release.version}…", operation, done)

    def restart(self, command: list[str]):
        """Lance la nouvelle version puis ferme celle-ci (les bibliothèques ont changé sur le disque)."""
        if not self.launcher(command[0], command[1:]):
            self.last_message += " Redémarrez MusicScope vous-même."
            self.window.state.setText(self.last_message)
            return
        self.window.close()
        app = QApplication.instance()
        if app is not None:
            app.quit()
