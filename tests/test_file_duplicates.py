"""Fichiers en double : critères combinables, aides de sélection, quarantaine et vidage."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from musicscope.cache import Cache
from musicscope.file_duplicates import (
    CONTENT, DATE, FINGERPRINT, NAME, SIZE, SONG, ScanOptions, collect_files, empty_quarantine, find_duplicates,
    fully_selected, quarantine_files, quarantine_summary, restore_file_journal, select_all_but_best,
    select_keep_by_date, select_lossy_when_lossless,
)
from musicscope.organizer import QUARANTINE_DIR, OrganizeError
from musicscope.recognition import fingerprint_similarity
from musicscope.tagwriter import _Tags

from test_tagwriter import SOURCES


def write(path: Path, data: bytes, mtime: int | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def audio(path: Path, fmt: str, artist="NxxxxxS", title="Hydrowaves") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(str(SOURCES[fmt]), path)
    tags = _Tags(path)
    for key, value in {"artist": [artist], "title": [title], "album": ["Album"], "tracknumber": ["1"]}.items():
        tags.set(key, value)
    tags.save()
    return path


def run(root, criteria, cache_path=None, **options):
    opts = ScanOptions(frozenset(criteria), **options)
    records = collect_files(root, opts)
    if cache_path is None:
        return find_duplicates(records, opts)
    with Cache(cache_path) as cache:
        return find_duplicates(records, opts, cache=cache)


def names(groups):
    return [sorted(os.path.relpath(r.path, r.path.split(os.sep + "lib" + os.sep)[0] + os.sep + "lib") for r in g.files)
            for g in groups]


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "lib"
    write(root / "A" / "cover.jpg", b"image-1", 1_600_000_000)
    write(root / "B" / "cover.jpg", b"image-1", 1_600_000_000)      # même nom, taille, date, contenu
    write(root / "C" / "front.jpg", b"image-1", 1_700_000_000)      # même contenu, autre nom et date
    write(root / "D" / "cover.jpg", b"image-2", 1_600_000_000)      # même nom, taille, date ; autre contenu
    write(root / "E" / "notes.txt", b"", 1_600_000_000)             # vide : ignoré par défaut
    write(root / "E" / ".cache.jpg", b"image-1", 1_600_000_000)     # caché : ignoré par défaut
    return root


def test_content_ignores_names_and_dates(lib, tmp_path):
    assert names(run(lib, {CONTENT}, tmp_path / "c.sqlite3")) == [["A/cover.jpg", "B/cover.jpg", "C/front.jpg"]]


def test_criteria_are_combined_with_and(lib, tmp_path):
    assert names(run(lib, {NAME, SIZE, DATE})) == [["A/cover.jpg", "B/cover.jpg", "D/cover.jpg"]]
    assert names(run(lib, {NAME, SIZE, DATE, CONTENT}, tmp_path / "c.sqlite3")) == [["A/cover.jpg", "B/cover.jpg"]]
    assert names(run(lib, {SIZE}))[0] == ["A/cover.jpg", "B/cover.jpg", "C/front.jpg", "D/cover.jpg"]


def test_filters_hidden_empty_size_extensions_and_folders(lib, tmp_path):
    assert ".cache.jpg" not in str(names(run(lib, {SIZE})))
    found = names(run(lib, {SIZE}, min_size=0, skip_hidden=False))
    assert ["E/notes.txt"] not in found and any("E/.cache.jpg" in group for group in found)
    assert run(lib, {SIZE}, min_size=100) == []
    assert run(lib, {SIZE}, max_size=3) == []
    assert run(lib, {SIZE}, include_extensions=frozenset({".png"})) == []
    assert names(run(lib, {CONTENT}, tmp_path / "c.sqlite3", exclude_folders=("C",))) == [["A/cover.jpg", "B/cover.jpg"]]
    assert run(lib, {CONTENT}, tmp_path / "c.sqlite3", exclude_extensions=frozenset({".jpg"})) == []


def test_quarantine_folder_and_links_are_ignored(lib, tmp_path):
    write(lib / QUARANTINE_DIR / "A" / "cover.jpg", b"image-1")
    os.symlink(lib / "A" / "cover.jpg", lib / "lien.jpg")
    group = run(lib, {CONTENT}, tmp_path / "c.sqlite3")[0]
    assert all(QUARANTINE_DIR not in r.path and not r.path.endswith("lien.jpg") for r in group.files)


def test_same_song_across_formats_and_duration_tolerance(tmp_path):
    root = tmp_path / "lib"
    audio(root / "2013 - NxxxxxS - Hydrowaves" / "01.flac", "flac")
    audio(root / "Hydrowaves (2013)" / "01.mp3", "mp3")
    audio(root / "Autre" / "01.ogg", "ogg", title="Autre titre")
    groups = run(root, {SONG})
    assert names(groups) == [["2013 - NxxxxxS - Hydrowaves/01.flac", "Hydrowaves (2013)/01.mp3"]]
    group = groups[0]
    assert group.best().codec == "FLAC"
    assert select_lossy_when_lossless(groups) == {str(root / "Hydrowaves (2013)" / "01.mp3")}


def test_options_reject_empty_or_unknown_criteria():
    with pytest.raises(ValueError):
        ScanOptions(frozenset())
    with pytest.raises(ValueError):
        ScanOptions(frozenset({"inconnu"}))


def test_selection_helpers_and_full_group_guard(lib, tmp_path):
    groups = run(lib, {CONTENT}, tmp_path / "c.sqlite3")
    files = {os.path.basename(os.path.dirname(r.path)): r.path for r in groups[0].files}
    all_but_best = select_all_but_best(groups)
    assert len(all_but_best) == 2 and groups[0].best().path not in all_but_best
    assert select_keep_by_date(groups, newest=True) == {files["A"], files["B"]}
    assert select_keep_by_date(groups, newest=False) == {files["B"], files["C"]}
    assert fully_selected(groups, set(files.values())) == groups
    assert fully_selected(groups, all_but_best) == []


class FakeFingerprinter:
    def __init__(self, prints):
        self.prints = prints

    def raw(self, track, cancel=None):
        return self.prints[os.path.basename(os.path.dirname(track.path))]


def test_fingerprint_groups_similar_recordings(tmp_path):
    root = tmp_path / "lib"
    audio(root / "A" / "01.flac", "flac", title="Sans tags fiables")
    audio(root / "B" / "01.mp3", "mp3", title="Autre nom")
    audio(root / "C" / "01.ogg", "ogg", title="Différent")
    base = [0x12345678 * (i + 1) & 0xFFFFFFFF for i in range(120)]
    noisy = [value ^ (1 << (i % 32)) for i, value in enumerate(base)]  # 1 bit sur 32 change
    other = [~value & 0xFFFFFFFF for value in base]
    opts = ScanOptions(frozenset({FINGERPRINT}))
    records = collect_files(root, opts)
    groups = find_duplicates(records, opts, fingerprinter=FakeFingerprinter(
        {"A": (60.0, base), "B": (60.5, noisy), "C": (60.0, other)}))
    assert names(groups) == [["A/01.flac", "B/01.mp3"]]
    with pytest.raises(ValueError):
        find_duplicates(records, opts)


def test_similarity_measure_handles_offsets():
    base = [(i * 2654435761) & 0xFFFFFFFF for i in range(200)]
    assert fingerprint_similarity(base, base) == 1.0
    assert fingerprint_similarity(base, base[3:]) == 1.0
    assert fingerprint_similarity(base, [~v & 0xFFFFFFFF for v in base]) < 0.75  # sans rapport : ~0,5, loin du seuil 0,85
    assert fingerprint_similarity(base[:4], base[:4]) == 0.0


@pytest.mark.skipif(shutil.which("fpcalc") is None or shutil.which("ffmpeg") is None, reason="fpcalc et ffmpeg requis")
def test_real_chromaprint_matches_flac_and_mp3_of_same_signal(tmp_path):
    from musicscope.recognition import Fingerprinter
    root = tmp_path / "lib"
    (root / "A").mkdir(parents=True)
    (root / "B").mkdir()
    (root / "C").mkdir()
    signal = ["-f", "lavfi", "-i", "sine=frequency=330:duration=15", "-f", "lavfi", "-i",
              "anoisesrc=d=15:a=0.08:seed=7", "-filter_complex", "amix=inputs=2", "-ac", "1", "-ar", "44100"]
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *signal, str(root / "A" / "01.flac")], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *signal, "-b:a", "96k", str(root / "B" / "01.mp3")], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anoisesrc=d=15:a=0.3:seed=99:c=pink",
                    "-ac", "1", "-ar", "44100", str(root / "C" / "01.flac")], check=True)
    opts = ScanOptions(frozenset({FINGERPRINT}))
    printer = Fingerprinter(tmp_path / "fp.sqlite3")
    try:
        groups = find_duplicates(collect_files(root, opts), opts, fingerprinter=printer)
    finally:
        printer.close()
    assert names(groups) == [["A/01.flac", "B/01.mp3"]]


def test_quarantine_restore_and_refusals(lib, tmp_path):
    groups = run(lib, {CONTENT}, tmp_path / "c.sqlite3")
    chosen = [r for r in groups[0].files if r.path in select_all_but_best(groups)]
    journal, moved, failed = quarantine_files(chosen, lib, tmp_path / "journals")
    assert not failed and len(moved) == 2
    for path in moved:
        assert not Path(path).exists()
        assert (lib / QUARANTINE_DIR / Path(path).relative_to(lib)).exists()
    folder, count, size = quarantine_summary(lib)
    assert (count, size) == (2, 14)
    assert run(lib, {CONTENT}, tmp_path / "c.sqlite3") == []
    restored, failed = restore_file_journal(journal)
    assert not failed and sorted(restored) == sorted(moved)
    assert all(Path(path).exists() for path in moved)
    stale = groups[0].files[0]
    os.utime(stale.path, (1, 1))
    _, moved, failed = quarantine_files([stale], lib, tmp_path / "journals")
    assert not moved and "modifié depuis l'analyse" in failed[0][1]


def test_quarantine_name_collision_and_restore_refused_when_occupied(lib, tmp_path):
    write(lib / QUARANTINE_DIR / "B" / "cover.jpg", b"ancien")
    record = next(r for g in run(lib, {CONTENT}, tmp_path / "c.sqlite3") for r in g.files if "/B/" in r.path)
    journal, moved, _ = quarantine_files([record], lib, tmp_path / "journals")
    assert (lib / QUARANTINE_DIR / "B" / "cover (2).jpg").exists()
    write(lib / "B" / "cover.jpg", b"nouveau")
    restored, failed = restore_file_journal(journal)
    assert not restored and "emplacement d'origine" in failed[0][1]


def test_empty_quarantine_requires_confirmation_and_only_touches_quarantine(lib, tmp_path):
    groups = run(lib, {CONTENT}, tmp_path / "c.sqlite3")
    chosen = [r for r in groups[0].files if r.path in select_all_but_best(groups)]
    quarantine_files(chosen, lib, tmp_path / "journals")
    with pytest.raises(OrganizeError):
        empty_quarantine(lib)
    before = sorted(p for p in lib.rglob("*") if QUARANTINE_DIR not in str(p))
    assert empty_quarantine(lib, confirmed=True) == (2, 14)
    assert not (lib / QUARANTINE_DIR).exists()
    assert sorted(lib.rglob("*")) == before
    assert empty_quarantine(lib, confirmed=True) == (0, 0)


def test_empty_quarantine_refuses_symlinked_folder(lib, tmp_path):
    outside = tmp_path / "ailleurs"
    write(outside / "precieux.flac", b"ne pas effacer")
    os.symlink(outside, lib / QUARANTINE_DIR)
    with pytest.raises(OrganizeError):
        empty_quarantine(lib, confirmed=True)
    assert (outside / "precieux.flac").exists()


def test_song_duration_tolerance_separates_different_lengths(tmp_path):
    from musicscope.file_duplicates import _split_by_duration, FileRecord
    from musicscope.models import Track
    records = [FileRecord(f"/m/{n}.flac", 1, 1, 1, n, Track(f"/m/{n}.flac", "FLAC", duration=d))
               for n, d in enumerate((200.0, 201.5, 230.0))]
    assert [[r.duration for r in g] for g in _split_by_duration(records, 2.0)] == [[200.0, 201.5]]
    assert _split_by_duration(records, 1.0) == []
