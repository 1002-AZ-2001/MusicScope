# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH)
analysis = Analysis(
    [str(root / "scripts" / "frozen_entry.py")],
    pathex=[str(root)],
    binaries=[],
    datas=collect_data_files("musicscope", includes=["data/*.flac", "data/*.mp3", "data/*.png"]),
    hiddenimports=collect_submodules("musicscope") + collect_submodules("mutagen"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "PyQt5", "PyQt6", "PySide2",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick", "PySide6.QtPdf", "PySide6.QtPdfWidgets",
        "PySide6.QtQuick", "PySide6.QtQuickWidgets", "PySide6.QtQml",
        "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets", "PySide6.QtTest",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(analysis.pure)
gui = EXE(
    pyz, analysis.scripts, [], exclude_binaries=True, name="MusicScope",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=True, disable_windowed_traceback=False,
)
executables = [gui]
bundle = COLLECT(
    *executables, analysis.binaries, analysis.datas,
    strip=False, upx=False, upx_exclude=[], name="MusicScope",
)
