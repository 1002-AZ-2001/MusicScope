"""Entrée explicite de PyInstaller, également valable sans gel."""
from musicscope.__main__ import main


if __name__ == "__main__":
    raise SystemExit(main())
