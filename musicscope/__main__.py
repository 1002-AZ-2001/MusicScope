"""Point d'entrée graphique et commandes utiles hors interface."""
import argparse
import json
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
    args = parser.parse_args()
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
        app.setApplicationVersion(__version__)
        from .gui import app_icon
        app.setWindowIcon(app_icon())
        app.setOrganizationName("MusicScope")
        window = MainWindow(args.cache)
        window.show()
        return app.exec()
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"MusicScope : {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
