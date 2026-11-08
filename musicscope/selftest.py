"""Autotest du paquet installé ou gelé, sans réseau ni données privées."""
import hashlib
import os
import tempfile
import time
from pathlib import Path


def self_test() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication
    from . import __version__
    from .cache import Cache
    from .comparison import IDENTICAL, MATCHING, compare_albums, exact_file_groups
    from .demo import create_demo
    from .scanner import scan
    from .advanced_gui import AdvancedWindow

    with tempfile.TemporaryDirectory(prefix="musicscope-selftest-") as temporary:
        directory = Path(temporary)
        root = create_demo(directory / "musique")
        before = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in root.rglob("*") if p.is_file()}
        with Cache(directory / "cache" / "inventory.sqlite3") as cache:
            inventory = scan(root, cache)
            albums = {Path(a.path).name: a for a in inventory.albums}
            assert not inventory.errors and len(albums) >= 7, "Inventaire de démonstration invalide"
            assert compare_albums(albums["Horizons FLAC"], albums["Copie noms différents"], cache, exact=True).status == IDENTICAL
            assert compare_albums(albums["Horizons FLAC"], albums["Horizons MP3"]).status == MATCHING
            assert exact_file_groups(inventory, cache), "Recherche SHA-256 non fonctionnelle"
        app = QApplication.instance() or QApplication([])
        window = AdvancedWindow(directory / "gui-cache")
        window.root = str(root)
        window.show()
        window.start_scan()
        deadline = time.monotonic() + 30
        while window.task is not None and time.monotonic() < deadline:
            app.processEvents()
            QThread.msleep(5)
        if window.task is not None:
            window.task.cancel.set()
            window.task.wait()
            app.processEvents()
            raise RuntimeError("Délai de l'autotest graphique dépassé")
        assert window.album_model.rowCount() >= 7, window.state.text()
        window.close()
        app.processEvents()
        after = {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in root.rglob("*") if p.is_file()}
        assert before == after, "L'audit a modifié les fichiers de démonstration"
    print(f"MusicScope {__version__} : auto-test réussi (scan, SHA-256, FLAC/MP3, Qt, lecture seule).")
    return 0
