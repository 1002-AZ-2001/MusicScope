"""Performance (v0.12.0) : régressions issues de l'audit.

Les tests comptent des opérations (appels système, hachages, comparaisons) plutôt que
des durées : ils restent stables d'une machine à l'autre.
"""
import json
import os
import random
import shutil
from pathlib import Path

import pytest

import musicscope
from musicscope import file_duplicates as fd
from musicscope import organizer as org
from musicscope.cache import Cache
from musicscope.models import Album, Track
from musicscope.scanner import scan

TONE = Path(musicscope.__file__).parent / "data" / "tone-a.flac"


def library(root, albums=3, tracks=4, covers=True):
    for album in range(albums):
        folder = root / f"Artiste {album}" / f"Album {album}"
        folder.mkdir(parents=True)
        for number in range(tracks):
            shutil.copy(TONE, folder / f"{number:02d}.flac")
        if covers:
            (folder / "cover.jpg").write_bytes(b"jpeg")
            (folder / "album.nfo").write_text("nfo")
    return root


# Scan
def test_warm_scan_needs_no_stat_beyond_the_directory_listing(tmp_path, monkeypatch):
    root = library(tmp_path / "lib")
    with Cache(tmp_path / "cache.sqlite3") as cache:
        scan(root, cache, workers=1)
        calls = {"stat": 0}
        original_stat, original_lstat = os.stat, os.lstat

        def counting(original):
            def call(path, *args, **kwargs):
                if not isinstance(path, int) and os.fspath(path).startswith(str(root)):
                    calls["stat"] += 1
                return original(path, *args, **kwargs)
            return call

        monkeypatch.setattr(os, "stat", counting(original_stat))
        monkeypatch.setattr(os, "lstat", counting(original_lstat))
        inventory = scan(root, cache, workers=1)
    assert sum(len(album.tracks) for album in inventory.albums) == 12
    # Avant : environ 4 stat par fichier audio, plus un par pochette et .nfo (54 018 pour 13 200 pistes).
    assert calls["stat"] <= 6  # racine et montage, une fois par scan ; les fichiers passent par DirEntry.stat


def test_parallel_discovery_finds_the_same_inventory(tmp_path):
    root = library(tmp_path / "lib", albums=6)
    with Cache(tmp_path / "a.sqlite3") as first, Cache(tmp_path / "b.sqlite3") as second:
        sequential = scan(root, first, workers=1)
        parallel = scan(root, second, workers=4)
    assert [album.path for album in sequential.albums] == [album.path for album in parallel.albums]
    assert [[track.path for track in album.tracks] for album in sequential.albums] == \
        [[track.path for track in album.tracks] for album in parallel.albums]


def test_same_size_rewrite_with_restored_mtime_is_reread(tmp_path):
    from mutagen.flac import FLAC
    root = tmp_path / "lib"
    (root / "A").mkdir(parents=True)
    path = root / "A" / "01.flac"
    shutil.copy(TONE, path)
    tags = FLAC(path)
    tags["title"] = ["Ancien titre"]
    tags.save()
    with Cache(tmp_path / "cache.sqlite3") as cache:
        assert scan(root, cache, workers=1).albums[0].tracks[0].tags["title"] == ["Ancien titre"]
        before = path.stat()
        tags = FLAC(path)
        tags["title"] = ["Nouveau titre"]  # même longueur : la marge garde la taille identique
        tags.save()
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))  # option « garder la date » de Mp3tag
        assert path.stat().st_size == before.st_size
        assert scan(root, cache, workers=1).albums[0].tracks[0].tags["title"] == ["Nouveau titre"]


def test_cover_art_is_not_converted_to_text():
    from musicscope.metadata import canonical_tags

    class Exploding:
        @property
        def text(self):
            raise AssertionError("pochette convertie en texte")

    assert canonical_tags({"APIC:cover": Exploding(), "covr": Exploding(), "©nam": ["Titre"]}) == {"title": ["Titre"]}


def test_mount_decision_follows_a_linked_parent_of_the_root(tmp_path):
    from musicscope.identity import MountCache
    real = tmp_path / "nas" / "Musique"
    real.mkdir(parents=True)
    link = tmp_path / "raccourci"
    try:
        link.symlink_to(tmp_path / "nas", target_is_directory=True)
    except OSError:
        pytest.skip("liens symboliques indisponibles")
    table = {str(tmp_path / "nas"): frozenset({"cifs"}), "/": frozenset({"ext4"})}
    mounts = MountCache(lambda: table, root=link / "Musique")
    assert not mounts.is_local(link / "Musique" / "a.flac", 1)  # partage, même vu par le lien
    assert mounts.reloads == 1
    mounts.is_local(link / "Musique" / "b.flac", 2)
    mounts.is_local(link / "Musique" / "c.flac", 1)
    assert mounts.reloads == 2  # un seul relevé par périphérique nouveau, pas à chaque alternance


# Fichiers en double
def records_in(groups):
    return [sorted(Path(record.path).name for record in group.files) for group in groups]


def test_duration_tolerance_does_not_chain_live_versions():
    durations = {"studio": 446.0, "remaster": 447.8, "live-1": 449.5, "live-2": 451.3, "live-3": 453.0}
    records = [fd.FileRecord(f"/lib/{name}.flac", 1, 1, 1, index,
                             Track(f"/lib/{name}.flac", duration=duration, tags={"artist": ["M"], "title": ["One"]}))
               for index, (name, duration) in enumerate(durations.items(), 1)]
    groups = fd.find_duplicates(records, fd.ScanOptions(frozenset({fd.SONG})))
    assert all(max(r.duration for r in group.files) - min(r.duration for r in group.files) <= 2
               for group in groups)
    assert ["live-3.flac"] not in records_in(groups) and len(groups) == 2


def test_name_criterion_matches_decomposed_and_composed_accents(tmp_path):
    for folder, name in (("A", "Café.flac"), ("B", "Café.flac")):
        (tmp_path / folder).mkdir()
        shutil.copy(TONE, tmp_path / folder / name)
    options = fd.ScanOptions(frozenset({fd.NAME}), min_size=1)
    assert len(fd.find_duplicates(fd.collect_files(tmp_path, options), options)) == 1


def test_hardlinked_paths_are_hashed_once(tmp_path, monkeypatch):
    for index in range(5):
        (tmp_path / "lib").mkdir(exist_ok=True)
        path = tmp_path / "lib" / f"{index}.flac"
        path.write_bytes(os.urandom(2048 + index))  # tailles différentes : aucune vraie copie
        (tmp_path / "torrents").mkdir(exist_ok=True)
        os.link(path, tmp_path / "torrents" / f"{index}.flac")
    calls = []
    original = Cache.hash_track
    monkeypatch.setattr(Cache, "hash_track", lambda self, track, cancel=None: calls.append(track.path)
                        or original(self, track, cancel))
    options = fd.ScanOptions(frozenset({fd.SIZE, fd.CONTENT}), min_size=1)
    with Cache(tmp_path / "cache.sqlite3") as cache:
        groups = fd.find_duplicates(fd.collect_files(tmp_path, options), options, cache)
    assert groups == [] and calls == []  # 10 chemins, 5 fichiers physiques, aucune vraie copie


def test_select_all_but_best_ranks_each_group_once(monkeypatch):
    calls = []
    original = fd.quality_key
    monkeypatch.setattr(fd, "quality_key", lambda record: calls.append(1) or original(record))
    group = fd.FileGroup([fd.FileRecord(f"/lib/{index}/cover.jpg", 10, 1, 1, index + 1) for index in range(300)],
                         (fd.NAME,))
    assert len(fd.select_all_but_best([group])) == 299
    assert len(calls) == 300  # avant : 300 × 300 (best() recalculé pour chaque fichier)


def test_fast_fingerprint_similarity_matches_the_reference():
    from musicscope.recognition import fingerprint_similarity, pack_fingerprint

    def reference(left, right, max_offset=12):
        best = 0.0
        for offset in range(-max_offset, max_offset + 1):
            a, b = left[max(offset, 0):], right[max(-offset, 0):]
            length = min(len(a), len(b))
            if length >= 8:
                errors = sum(bin(x ^ y).count("1") for x, y in zip(a[:length], b[:length]))
                best = max(best, 1 - errors / (32 * length))
        return best

    rng = random.Random(4)
    for _ in range(25):
        left = [rng.getrandbits(32) for _ in range(rng.randint(5, 400))]
        right = [value ^ (rng.getrandbits(32) if rng.random() < 0.2 else 0) for value in left[rng.randint(0, 5):]]
        assert fingerprint_similarity(left, right) == pytest.approx(reference(left, right), abs=1e-12)
        assert fingerprint_similarity(pack_fingerprint(left), pack_fingerprint(right)) == \
            pytest.approx(reference(left, right), abs=1e-12)


def test_fingerprint_only_search_on_a_huge_library_is_refused_before_fpcalc():
    records = [fd.FileRecord(f"/lib/{index}.flac", 10, 1, 1, index + 1, Track(f"/lib/{index}.flac", duration=200.0))
               for index in range(1500)]

    class Printer:
        def raw(self, track, cancel=None):
            raise AssertionError("fpcalc ne doit pas être lancé")

    with pytest.raises(ValueError, match="Combinez-la"):
        fd.find_duplicates(records, fd.ScanOptions(frozenset({fd.FINGERPRINT})), None, Printer())


def test_fingerprint_cache_is_binary_reads_old_entries_and_purges_missing(tmp_path):
    from musicscope.recognition import Fingerprinter, _decode_raw, _encode_raw
    blob = _encode_raw(123.5, [1, 2, 3, 0xFFFFFFFF])
    assert _decode_raw(blob) == (123.5, [1, 2, 3, 0xFFFFFFFF]) and len(blob) == 5 + 8 + 16
    assert _decode_raw(json.dumps({"duration": 1.0, "fingerprint": [7]})) == (1.0, [7])
    printer = Fingerprinter(tmp_path / "fp.sqlite3")
    try:
        root = tmp_path / "lib"
        for name in ("kept", "gone"):
            printer.db.execute("INSERT INTO raw_fingerprints VALUES (?,?,?,?)", (str(root / f"{name}.flac"), 1, 1, blob))
        printer.db.execute("INSERT INTO raw_fingerprints VALUES (?,?,?,?)", ("/ailleurs/x.flac", 1, 1, blob))
        printer.db.commit()
        assert printer.purge_missing(root, {str(root / "kept.flac")}) == 1
        assert {row[0] for row in printer.db.execute("SELECT path FROM raw_fingerprints")} == \
            {str(root / "kept.flac"), "/ailleurs/x.flac"}
    finally:
        printer.close()


def test_file_quarantine_journal_is_written_per_batch(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    records = []
    for index in range(130):
        path = root / "A" / f"{index}.flac"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 10)
        info = path.stat()
        records.append(fd.FileRecord(str(path), info.st_size, info.st_mtime_ns, info.st_dev, info.st_ino))
    saves = []
    original = fd._save_journal
    monkeypatch.setattr(fd, "_save_journal", lambda path, data: saves.append(1) or original(path, data))
    journal, moved, failed = fd.quarantine_files(records, root, tmp_path / "journals")
    assert len(moved) == 130 and not failed
    assert len(saves) == 6  # 3 lots × (intentions + états) ; avant : 2 par fichier, soit 260
    restored, failed = fd.restore_file_journal(journal)
    assert len(restored) == 130 and not failed


# Rangement
def test_organizer_hashes_each_file_once_per_move_and_restore(tmp_path, monkeypatch):
    source = tmp_path / "lib" / "Old"
    source.mkdir(parents=True)
    tracks = []
    for number in (1, 2):
        path = source / f"0{number}.flac"
        shutil.copy(TONE, path)
        info = path.stat()
        tracks.append(Track(str(path), size=info.st_size, mtime_ns=info.st_mtime_ns,
                            tags={"title": ["T"], "album": ["Alb"], "albumartist": ["Art"], "artist": ["Art"],
                                  "date": ["2020"], "tracknumber": [str(number)]}))
    hashes = []
    original = org._hash_file
    monkeypatch.setattr(org, "_hash_file", lambda path, initial, cancel=None: hashes.append(path)
                        or original(path, initial, cancel))
    plan = org.plan_album(Album(str(source), tracks), tmp_path / "lib")
    assert len(hashes) == 2
    journal = org.apply_plan(plan, tmp_path / "journals", confirmed=True)
    assert len(hashes) == 2  # avant : 4 de plus
    org.restore_journal(journal, confirmed=True)
    assert len(hashes) == 4  # une lecture de contrôle, avant : 4


def test_generic_album_names_do_not_explode_into_pairs():
    from musicscope.comparison import candidates
    albums = [Album(f"/lib/Unknown Artist/Unknown Album {index}",
                    [Track(f"/lib/u{index}/01.flac", tags={"album": ["Unknown Album"], "albumartist": ["Unknown Artist"]})])
              for index in range(120)]
    assert candidates(albums) == []
    named = [Album(f"/lib/A/Greatest Hits {index}",
                   [Track(f"/lib/g{index}/01.flac", tags={"album": ["Greatest Hits"], "albumartist": ["Queen"]})])
             for index in range(3)]
    assert len(candidates(named)) == 3


@pytest.mark.parametrize("name,title,year", [
    ("Random Access Memories (2013) [FLAC]", "randomaccessmemories", "2013"),
    ("Random Access Memories (2013) [24-96]", "randomaccessmemories", "2013"),
    ("Random Access Memories (2013) - FLAC", "randomaccessmemories", "2013"),
    ("(2013) Random Access Memories", "randomaccessmemories", "2013"),
    ("Random Access Memories (2013)/Disc 1 - Bonus", "randomaccessmemories", "2013"),
    ("Album (2010) [WEB] [FLAC 24bit 96kHz]", "album", "2010"),
    ("1999 (2003)", "1999", "2003"),
])
def test_folder_names_with_technical_suffixes(name, title, year):
    from musicscope.folder_names import parse_folder
    parsed = parse_folder(f"/m/Daft Punk/{name}")
    assert (parsed.key()[1], parsed.year) == (title, year)


# Services
def test_acoustid_cached_answer_expires(tmp_path, monkeypatch):
    import time
    from musicscope.recognition import AcoustIDClient, Fingerprint
    answer = json.dumps({"status": "ok", "results": []}).encode()
    requests = []
    monkeypatch.setattr("musicscope.recognition._read_url", lambda url, opener=None: requests.append(url) or answer)
    client = AcoustIDClient("cle", cache_path=tmp_path / "acoustid.sqlite3")
    fingerprint = Fingerprint(200.0, "AQAAAA")
    now = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    for day in range(0, 30):
        now[0] = 1_000_000.0 + day * 86400
        client.lookup(fingerprint)
    assert len(requests) == 5  # jours 0, 7, 14, 21, 28 ; avant : 1 seule en 30 jours


def test_rows_model_filters_and_sorts_without_qt_items():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    from musicscope.gui import RowsModel
    QApplication.instance() or QApplication([])
    model = RowsModel(["Statut", "Chemin"])
    model.set_rows([("OK", "/b"), ("Absent", "/c"), ("OK", "/a")])
    model.sort(1, Qt.SortOrder.AscendingOrder)
    assert [row[1] for row in model.rows] == ["/a", "/b", "/c"]
    model.set_filter(lambda row: row[0] != "OK")
    assert model.rowCount() == 1 and model.item(0, 1).text() == "/c" and len(model.all_rows) == 3
