import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from musicscope.demo import create_demo
from musicscope.export import export_csv
from musicscope.paths import check_cache_outside_library


def run_cli(*args):
    return subprocess.run([sys.executable, "-m", "musicscope", *map(str, args)],
                          capture_output=True, text=True, timeout=20)


def test_cli_demo_and_real_offline_scan(tmp_path):
    library = tmp_path / "synthétique"
    created = run_cli("--demo", library)
    assert created.returncode == 0, created.stderr
    result = run_cli("--cache", tmp_path / "cache", "--scan", library)
    assert result.returncode == 0, result.stderr
    inventory = json.loads(result.stdout)
    assert inventory["root"] == str(library)
    assert len(inventory["albums"]) >= 7
    assert sum(len(a["tracks"]) for a in inventory["albums"]) >= 14
    assert not inventory["errors"]
    assert any(Path(a["path"]).name == "Concert" and len(a["tracks"]) == 2 for a in inventory["albums"])
    assert {t["codec"] for a in inventory["albums"] for t in a["tracks"]} >= {"FLAC", "MP3"}
    assert (tmp_path / "cache" / "inventory.sqlite3").is_file()


def test_demo_refuses_nonempty_destination_without_overwriting(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    sentinel = existing / "important.txt"
    sentinel.write_text("préserver", encoding="utf-8")
    with pytest.raises(ValueError, match="vide"):
        create_demo(existing)
    assert sentinel.read_text("utf-8") == "préserver"
    assert list(existing.iterdir()) == [sentinel]


def test_cli_errors_and_cache_cannot_write_inside_library(tmp_path):
    library = create_demo(tmp_path / "musique")
    cache = library / "cache"
    result = run_cli("--cache", cache, "--scan", library)
    assert result.returncode == 1
    assert "hors de la médiathèque" in result.stderr
    assert not cache.exists()
    result = run_cli("--cache", tmp_path / "cache", "--scan", tmp_path / "missing")
    assert result.returncode == 1
    assert "MusicScope" in result.stderr


def test_cache_guard_resolves_links(tmp_path):
    library = tmp_path / "music"
    library.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(library, target_is_directory=True)
    except OSError:
        pytest.skip("Liens symboliques indisponibles")
    with pytest.raises(ValueError):
        check_cache_outside_library(alias / "cache", library)


@pytest.mark.parametrize("value", ["=1+1", "+SUM(A1:A2)", "-1+2", "@SUM(1)", "  =2+2", "\ttext", "\r=2"])
def test_export_escapes_spreadsheet_formulas(tmp_path, value):
    path = tmp_path / "export.csv"
    export_csv(path, [{"catégorie": "Test", "actuel": value}])
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    with path.open(encoding="utf-8-sig", newline="") as stream:
        row = next(csv.DictReader(stream, delimiter=";"))
    assert row["actuel"] == "'" + value


def test_export_preserves_unicode_newlines_and_quotes(tmp_path):
    path = tmp_path / "rapport.csv"
    value = 'Björk; 東京\n"Édition"'
    export_csv(path, [{"catégorie": "Métadonnées", "actuel": value}])
    with path.open(encoding="utf-8-sig", newline="") as stream:
        assert next(csv.DictReader(stream, delimiter=";"))["actuel"] == value
