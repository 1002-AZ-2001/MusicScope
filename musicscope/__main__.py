"""Point d'entrée graphique et commandes utiles hors interface."""
import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .paths import check_cache_outside_library, configure_system_trust, default_cache_dir
from . import __version__


def main() -> int:
    configure_system_trust()
    parser = argparse.ArgumentParser(description="MusicScope — audit musical en lecture seule")
    parser.add_argument("--version", action="version", version=f"MusicScope {__version__}")
    parser.add_argument("--self-test", action="store_true", help="Vérifier le moteur et Qt sur un corpus temporaire")
    parser.add_argument("--cache", type=Path, default=default_cache_dir(), help="Cache local, hors médiathèque")
    parser.add_argument("--demo", type=Path, metavar="DOSSIER", help="Créer le jeu synthétique dans un dossier vide")
    parser.add_argument("--scan", type=Path, metavar="RACINE", help="Inventorier sans interface et afficher du JSON")
    parser.add_argument("--workers", type=int, metavar="N", help="Threads de lecture pour --scan (défaut : automatique)")
    parser.add_argument("--nas-work", type=Path, metavar="RACINE",
                        help="Sur le NAS : tags, SHA-256 et spectre, exportés pour l'interface (voir docs/truenas.md)")
    parser.add_argument("--out", type=Path, metavar="DOSSIER", help="Dossier du fichier musicscope-nas.json.gz")
    parser.add_argument("--spectrum", choices=("one", "all", "none"), default="one",
                        help="Spectre des FLAC : une piste par album, toutes, ou aucun")
    parser.add_argument("--no-hash", action="store_true", help="Ne pas calculer les SHA-256")
    parser.add_argument("--every", type=float, metavar="HEURES", help="Recommencer toutes les N heures")
    parser.add_argument("--check-update", action="store_true",
                        help="Afficher la dernière version publiée et le mode de mise à jour de cette copie")
    args = parser.parse_args()
    if args.nas_work:
        # Mode sans interface : n'importe pas Qt (le conteneur du NAS ne l'installe pas).
        if args.out is None:
            parser.error("--nas-work demande --out DOSSIER")
        if args.every is not None and not args.every > 0:
            parser.error("--every demande un nombre d'heures positif")
        from .nas import main as nas_main
        try:
            check_cache_outside_library(args.cache, args.nas_work)
            check_cache_outside_library(args.out, args.nas_work)
        except ValueError as exc:
            print(f"MusicScope : {exc}", file=sys.stderr)
            return 1
        return nas_main(args)
    if args.check_update:
        from .updater import UpdateError, detect_installation, fetch_latest
        installation = detect_installation()
        print(f"MusicScope {__version__}. Installation : {installation.kind}. {installation.detail}", flush=True)
        try:
            release = fetch_latest()
        except UpdateError as exc:
            print(f"MusicScope : {exc}", file=sys.stderr)
            return 1
        state = "nouvelle version disponible" if release.newer else "à jour"
        print(f"Dernière version publiée : {release.version} ({state}) : {release.page}")
        return 0
    try:
        if args.self_test:
            from .selftest import self_test
            return self_test()
        if args.demo:
            from .demo import create_demo
            print(create_demo(args.demo))
            return 0
        if args.scan:
            check_cache_outside_library(args.cache, args.scan)
        args.cache.mkdir(parents=True, exist_ok=True)
        if args.scan:
            from .cache import Cache
            from .scanner import scan
            cache = Cache(args.cache / "inventory.sqlite3")
            try:
                inventory = scan(args.scan, cache, workers=args.workers)
                print(json.dumps(inventory.to_dict(), ensure_ascii=False, indent=2))
            finally:
                cache.close()
            return 0
        from PySide6.QtWidgets import QApplication
        from .advanced_gui import AdvancedWindow as MainWindow
        app = QApplication(sys.argv[:1])
        app.setApplicationName("MusicScope")
        app.setStyle("Fusion")
        from .gui import dark_palette, install_french_translation
        app.setPalette(dark_palette())
        install_french_translation(app)
        app.setApplicationVersion(__version__)
        from .gui import app_icon
        app.setWindowIcon(app_icon())
        app.setOrganizationName("MusicScope")
        window = MainWindow(args.cache)
        window.show()
        if not os.environ.get("MUSICSCOPE_NO_UPDATE_CHECK"):
            from PySide6.QtCore import QTimer
            QTimer.singleShot(4000, window.updater.check_on_startup)  # après l'affichage, sans le ralentir
        return app.exec()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"MusicScope : {exc}", file=sys.stderr)
        return 1
    except sqlite3.Error as exc:
        print(f"MusicScope : base locale illisible dans {args.cache} ({exc}). Renommez ou supprimez le fichier "
              ".sqlite3 en cause, puis relancez.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
