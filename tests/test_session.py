from dataclasses import asdict
import json
from pathlib import PurePosixPath, PureWindowsPath

import pytest

from musicscope.comparison import Comparison, IDENTICAL
from musicscope.models import Album, Inventory, Track
from musicscope.review import Decision
from musicscope.session import export_session, import_session


def inventory(root="/music"):
    # Ce sont des chemins historiques, indépendants du système qui exécute
    # les tests. Path('/music') serait un chemin Windows sans lecteur sous CI.
    historical_root = (PureWindowsPath(root) if root.startswith("\\\\")
                       or (len(root) >= 2 and root[1] == ":") else PurePosixPath(root))
    albums = []
    for folder in ("A", "B"):
        album_path = historical_root / folder
        path = str(album_path)
        albums.append(Album(path, [Track(str(album_path / "01.flac"), "FLAC", 12, 2000, 1.0,
            {"album": ["Horizons"], "title": ["Aube"], "albumartist": ["東京"]}, 1, 42)]))
    return Inventory(root, albums, scanned_at="2026-11-08T12:00:00+00:00")


@pytest.mark.parametrize("root,expected", [("/music", "/music/A/01.flac"),
                                           (r"C:\Musique", r"C:\Musique\A\01.flac"),
                                           (r"\\truenas\Musique", r"\\truenas\Musique\A\01.flac")])
def test_historical_paths_do_not_depend_on_host_platform(root, expected):
    assert inventory(root).albums[0].tracks[0].path == expected


def export_fixture(tmp_path, inv=None):
    inv = inv or inventory()
    left, right = inv.albums
    decision = Decision(left.path, right.path, "Conserver les deux", "Première édition", "2026-11-08T12:00:00+00:00", False, "a", "b")
    path = tmp_path / "session.json"
    export_session(path, inv, [(left, right, Comparison(IDENTICAL, ["SHA-256"], ["Codecs différents"]))],
                   [[left.tracks[0], right.tracks[0]]], [decision])
    return path


def test_session_roundtrip_historical_and_no_identity_reuse(tmp_path):
    path = export_fixture(tmp_path)
    result = import_session(path)
    assert result.historical
    assert result.original_root == "/music"
    assert result.inventory.root == "/music"
    assert result.inventory.albums[0].value("albumartist") == "東京"
    assert all(t.device is None and t.inode is None for a in result.inventory.albums for t in a.tracks)
    assert result.comparisons[0]["status"] == IDENTICAL
    assert result.comparisons[0]["historical"] is True
    assert result.decisions[0]["stale"] is True
    assert len(result.exact_groups[0]) == 2
    assert result.exact_groups[0][0] is result.inventory.albums[0].tracks[0]


def test_session_explicit_root_mapping_preserves_original_paths(tmp_path):
    path = export_fixture(tmp_path)
    result = import_session(path, [("/music", "/mounted/music")])
    assert result.inventory.root == "/mounted/music"
    assert result.inventory.albums[0].path == "/mounted/music/A"
    assert result.original_paths["/mounted/music/A/01.flac"] == "/music/A/01.flac"
    assert result.comparisons[0]["left_path"] == "/mounted/music/A"
    assert result.decisions[0]["left_path"] == "/mounted/music/A"
    assert result.exact_groups[0][0].path == "/mounted/music/A/01.flac"


def test_session_longest_mapping_applied_with_safe_boundaries(tmp_path):
    path = export_fixture(tmp_path)
    result = import_session(path, [("/music", "/mounted"), ("/music/A", "/mounted/renamed")])
    assert result.inventory.albums[0].path == "/mounted/renamed"
    assert result.inventory.albums[1].path == "/mounted/B"
    with pytest.raises(ValueError, match="racine"):
        import_session(path, [("/musical", "/mounted")])


def test_session_windows_unc_to_linux_mapping(tmp_path):
    inv = inventory(r"\\truenas\Musique")
    path = export_fixture(tmp_path, inv)
    result = import_session(path, [(r"\\TRUENAS\Musique", "/mnt/Musique")])
    assert result.inventory.albums[0].path == "/mnt/Musique/A"
    assert result.original_root == r"\\truenas\Musique"


@pytest.mark.parametrize("kind", ["schema", "source_path", "track_path", "duplicate", "unknown_config", "negative_size", "nan_duration", "bad_tags", "comparison_path", "group_path", "decision_path"])
def test_session_rejects_malformed_or_out_of_scope_payload(tmp_path, kind):
    path = export_fixture(tmp_path)
    payload = json.loads(path.read_text("utf-8"))
    if kind == "schema":
        payload["schema_version"] = 99
    elif kind == "source_path":
        payload["inventory"]["albums"][0]["path"] = "/music/../outside"
    elif kind == "track_path":
        payload["inventory"]["albums"][0]["tracks"][0]["path"] = "/outside/01.flac"
    elif kind == "duplicate":
        payload["inventory"]["albums"].append(payload["inventory"]["albums"][0])
    elif kind == "unknown_config":
        payload["api_key"] = "never-import-credentials"
    elif kind == "negative_size":
        payload["inventory"]["albums"][0]["tracks"][0]["size"] = -1
    elif kind == "nan_duration":
        payload["inventory"]["albums"][0]["tracks"][0]["duration"] = float("nan")
    elif kind == "bad_tags":
        payload["inventory"]["albums"][0]["tracks"][0]["tags"]["title"] = "not a list"
    elif kind == "comparison_path":
        payload["comparisons"][0]["left_path"] = "/music/Missing"
    elif kind == "group_path":
        payload["exact_groups"][0][0] = "/music/missing.flac"
    else:
        payload["decisions"][0]["left_path"] = "/outside/album"
    path.write_text(json.dumps(payload), "utf-8")
    with pytest.raises(ValueError):
        import_session(path)


def test_session_rejects_unknown_version_boolean_and_oversize(tmp_path, monkeypatch):
    path = export_fixture(tmp_path)
    payload = json.loads(path.read_text("utf-8"))
    payload["schema_version"] = True
    path.write_text(json.dumps(payload), "utf-8")
    with pytest.raises(ValueError):
        import_session(path)
    monkeypatch.setattr("musicscope.session.MAX_SESSION_BYTES", 10)
    with pytest.raises(ValueError, match="volumineuse"):
        import_session(path)


def test_session_rejects_invalid_mapping_without_writing_sources(tmp_path):
    path = export_fixture(tmp_path)
    before = path.read_bytes()
    for mappings in [[("/music", "/outside/../mounted")], [("/music/A", "/mounted")],
                     [("/music", "/mounted"), ("/music", "/other")],
                     [("/music", "/mounted"), ("/music/A", "/mounted/B")]]:
        with pytest.raises(ValueError):
            import_session(path, mappings)
    assert path.read_bytes() == before


def test_session_exports_no_configuration_and_refuses_audio_target(tmp_path):
    path = export_fixture(tmp_path)
    payload = json.loads(path.read_text("utf-8"))
    assert set(payload) == {"schema_version", "app_version", "rules_version", "exported_at", "inventory", "comparisons", "exact_groups", "decisions"}
    with pytest.raises(ValueError, match="JSON"):
        export_session(tmp_path / "audio.flac", inventory())
    assert not (tmp_path / "audio.flac").exists()


def test_session_bad_json_is_rejected(tmp_path):
    path = tmp_path / "bad.json"
    path.write_bytes(b"{not-json")
    with pytest.raises(ValueError, match="invalide"):
        import_session(path)


def test_session_reexport_preserves_provenance_dates_and_historical_proofs(tmp_path):
    first = export_fixture(tmp_path)
    imported = import_session(first, [("/music", "/mounted/music")])
    second = tmp_path / "second.json"
    export_session(second, imported.inventory, source_session=imported)
    payload = json.loads(second.read_text("utf-8"))
    assert payload["provenance"]["historical"] is True
    assert payload["comparisons"][0]["historical"] is True
    assert payload["exported_at"] == imported.exported_at
    again = import_session(second, [("/mounted/music", "/another/music")])
    assert again.original_root == "/music"
    assert again.original_paths["/another/music/A/01.flac"] == "/music/A/01.flac"
    assert again.path_mappings == [("/music", "/mounted/music"), ("/mounted/music", "/another/music")]
    assert again.comparisons[0]["left_path"] == "/another/music/A"
    assert again.decisions[0]["stale"]
    assert again.exact_groups[0][0].path == "/another/music/A/01.flac"


def test_session_untrusted_provenance_cannot_inject_config_or_paths(tmp_path):
    path = export_fixture(tmp_path)
    data = json.loads(path.read_text("utf-8"))
    data["provenance"] = {"historical": True, "original_root": "/music", "original_paths": {"/music/A": "/outside/A"}, "path_mappings": []}
    path.write_text(json.dumps(data), "utf-8")
    with pytest.raises(ValueError, match="provenance"):
        import_session(path)


def test_session_refuses_mapping_track_outside_its_album(tmp_path):
    path = export_fixture(tmp_path)
    with pytest.raises(ValueError, match="hors de son album"):
        import_session(path, [("/music", "/mounted"), ("/music/A/01.flac", "/mounted/elsewhere.flac")])
