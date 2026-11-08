"""Sessions JSON versionnées : import historique, sans action sur les sources."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import tempfile

from . import __version__
from .models import Inventory, Track
from .review import DECISIONS, RULES_VERSION

SCHEMA_VERSION = 1
MAX_SESSION_BYTES = 64 * 1024 * 1024
MAX_TRACKS = 500000


@dataclass
class Session:
    inventory: Inventory
    comparisons: list[dict]
    exact_groups: list[list[Track]]
    decisions: list[dict]
    exported_at: str
    rules_version: str
    historical: bool = True
    original_root: str = ""
    original_paths: dict[str, str] = field(default_factory=dict)
    path_mappings: list[tuple[str, str]] = field(default_factory=list)
    app_version: str = ""


def _path(value):
    if not isinstance(value, str) or not value or len(value) > 32768 or "\0" in value:
        raise ValueError("Chemin de session invalide.")
    windows = bool(re.match(r"^[a-zA-Z]:|^\\\\|^//", value))
    parsed = PureWindowsPath(value) if windows else PurePosixPath(value)
    if not parsed.is_absolute() or ".." in parsed.parts:
        raise ValueError("Les chemins de session doivent être absolus et sans '..'.")
    return parsed


def _contained(value: str, parent: str):
    child, root = _path(value), _path(parent)
    return type(child) is type(root) and child.is_relative_to(root)


def _text(value, maximum=100000):
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("Texte de session invalide ou trop volumineux.")
    return value


def _strings(value, maximum=100000):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError("Liste de session invalide.")
    for item in value:
        _text(item)
    return value


def _validate_track(track):
    if not isinstance(track, dict):
        raise ValueError("Piste de session invalide.")
    if set(track) - {"path", "codec", "size", "mtime_ns", "duration", "tags", "device", "inode"}:
        raise ValueError("Champs de piste inconnus.")
    _path(track.get("path"))
    _text(track.get("codec", ""), 100)
    for key in ("size", "mtime_ns"):
        value = track.get(key)
        if type(value) is not int or (key == "size" and value < 0):
            raise ValueError("Taille ou date de modification de piste invalide.")
    duration = track.get("duration")
    if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0):
        raise ValueError("Durée de piste invalide.")
    tags = track.get("tags", {})
    if not isinstance(tags, dict) or len(tags) > 1000:
        raise ValueError("Tags de session invalides.")
    for key, values in tags.items():
        _text(key, 1000)
        _strings(values, 1000)
    for key in ("device", "inode"):
        if track.get(key) is not None and (type(track[key]) is not int or track[key] < 0):
            raise ValueError("Identité de piste invalide.")


def _validate(payload):
    if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != SCHEMA_VERSION):
        raise ValueError("Version de session inconnue.")
    allowed = {"schema_version", "app_version", "rules_version", "exported_at", "inventory", "comparisons", "exact_groups", "decisions", "provenance"}
    if set(payload) - allowed:
        raise ValueError("Champs de session inconnus ; aucune configuration distante ne peut être importée.")
    for key in ("app_version", "rules_version", "exported_at"):
        _text(payload.get(key), 1000)
    inventory = payload.get("inventory")
    if not isinstance(inventory, dict) or set(inventory) - {"root", "albums", "errors", "scanned_at"}:
        raise ValueError("Inventaire de session invalide.")
    root = inventory.get("root")
    _path(root)
    _strings(inventory.get("errors", []))
    _text(inventory.get("scanned_at", ""), 1000)
    albums = inventory.get("albums")
    if not isinstance(albums, list) or len(albums) > MAX_TRACKS:
        raise ValueError("Liste d'albums invalide ou trop volumineuse.")
    album_paths, tracks = set(), {}
    for album in albums:
        if not isinstance(album, dict) or set(album) - {"path", "tracks", "complete", "issues"}:
            raise ValueError("Album de session invalide.")
        path = album.get("path")
        if not _contained(path, root) or _path(path) in album_paths:
            raise ValueError("Album hors de la racine ou dossier répété.")
        album_paths.add(_path(path))
        if type(album.get("complete")) is not bool:
            raise ValueError("Complétude d'album invalide.")
        _strings(album.get("issues", []))
        if not isinstance(album.get("tracks"), list):
            raise ValueError("Liste de pistes invalide.")
        for track in album["tracks"]:
            _validate_track(track)
            track_path = track["path"]
            if not _contained(track_path, path) or _path(track_path) in tracks:
                raise ValueError("Piste hors de son album ou chemin répété.")
            tracks[_path(track_path)] = track
            if len(tracks) > MAX_TRACKS:
                raise ValueError("Trop de pistes dans la session.")
    comparisons = payload.get("comparisons", [])
    if not isinstance(comparisons, list) or len(comparisons) > MAX_TRACKS:
        raise ValueError("Comparaisons de session invalides.")
    for comparison in comparisons:
        if not isinstance(comparison, dict) or set(comparison) - {"left_path", "right_path", "status", "reasons", "differences", "historical"}:
            raise ValueError("Comparaison de session invalide.")
        if _path(comparison.get("left_path")) not in album_paths or _path(comparison.get("right_path")) not in album_paths:
            raise ValueError("Comparaison référençant un album absent.")
        _text(comparison.get("status"), 1000)
        _strings(comparison.get("reasons", []))
        _strings(comparison.get("differences", []))
    exact_groups = payload.get("exact_groups", [])
    if not isinstance(exact_groups, list) or len(exact_groups) > MAX_TRACKS:
        raise ValueError("Groupes SHA de session invalides.")
    for group in exact_groups:
        if not isinstance(group, list) or len(group) > MAX_TRACKS or any(_path(path) not in tracks for path in group):
            raise ValueError("Groupe SHA référençant une piste absente.")
        if len({_path(path) for path in group}) != len(group):
            raise ValueError("Chemin répété dans un groupe SHA.")
    decisions = payload.get("decisions", [])
    if not isinstance(decisions, list) or len(decisions) > MAX_TRACKS:
        raise ValueError("Décisions de session invalides.")
    decision_fields = {"left_path", "right_path", "decision", "note", "recorded_at", "stale", "left_signature", "right_signature", "rules_version"}
    for decision in decisions:
        if not isinstance(decision, dict) or set(decision) - decision_fields or decision.get("decision") not in DECISIONS:
            raise ValueError("Décision de session invalide.")
        if not _contained(decision.get("left_path"), root) or not _contained(decision.get("right_path"), root):
            raise ValueError("Décision hors du périmètre de session.")
        for key in ("note", "recorded_at", "left_signature", "right_signature", "rules_version"):
            _text(decision.get(key, ""))
    provenance = payload.get("provenance")
    if provenance is not None:
        if (not isinstance(provenance, dict)
                or set(provenance) != {"historical", "original_root", "original_paths", "path_mappings"}
                or provenance.get("historical") is not True):
            raise ValueError("Provenance de session invalide.")
        original_root = provenance["original_root"]
        _path(original_root)
        originals = provenance["original_paths"]
        if not isinstance(originals, dict) or len(originals) > 2 * MAX_TRACKS:
            raise ValueError("Provenance des chemins invalide.")
        declared = album_paths | set(tracks)
        for current, original in originals.items():
            if _path(current) not in declared or not _contained(original, original_root):
                raise ValueError("Chemin de provenance hors de son périmètre.")
        if {_path(current) for current in originals} != declared:
            raise ValueError("La provenance doit conserver tous les chemins de l'inventaire.")
        if not isinstance(provenance["path_mappings"], list) or len(provenance["path_mappings"]) > MAX_TRACKS:
            raise ValueError("Historique des correspondances invalide.")
        for mapping in provenance["path_mappings"]:
            if not isinstance(mapping, list) or len(mapping) != 2:
                raise ValueError("Correspondance historique invalide.")
            _path(mapping[0])
            _path(mapping[1])
    return payload


def export_session(path: str | Path, inventory: Inventory, matches=(), exact_groups=(), decisions=(),
                   source_session: Session | None = None):
    comparisons = []
    for left, right, result in matches:
        comparisons.append({"left_path": left.path, "right_path": right.path, "status": result.status,
                            "reasons": list(result.reasons), "differences": list(result.differences), "historical": False})
    payload = {"schema_version": SCHEMA_VERSION, "app_version": __version__, "rules_version": RULES_VERSION,
               "exported_at": datetime.now(timezone.utc).isoformat(), "inventory": inventory.to_dict(),
               "comparisons": comparisons, "exact_groups": [[track.path for track in group] for group in exact_groups],
               "decisions": [asdict(value) if is_dataclass(value) else dict(value) for value in decisions]}
    if source_session is not None:
        if inventory.root != source_session.inventory.root:
            raise ValueError("La session historique ne correspond pas à cet inventaire.")
        payload["rules_version"] = source_session.rules_version
        payload["exported_at"] = source_session.exported_at
        payload["app_version"] = source_session.app_version
        payload["comparisons"] = comparisons or source_session.comparisons
        for comparison in payload["comparisons"]:
            comparison["historical"] = True
        payload["exact_groups"] = payload["exact_groups"] or [[track.path for track in group] for group in source_session.exact_groups]
        payload["decisions"] = payload["decisions"] or source_session.decisions
        payload["provenance"] = {"historical": True, "original_root": source_session.original_root,
                                 "original_paths": source_session.original_paths,
                                 "path_mappings": [list(mapping) for mapping in source_session.path_mappings]}
    _validate(payload)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    if len(encoded) > MAX_SESSION_BYTES:
        raise ValueError("Session trop volumineuse pour être exportée.")
    target = Path(path)
    if target.suffix.casefold() != ".json" or target.is_symlink():
        raise ValueError("Choisissez un fichier JSON ordinaire pour exporter la session.")
    # Ne pas résoudre/stat des chemins historiques : ils peuvent désigner un
    # NAS absent ou une autre plateforme. Leur comparaison reste lexicale.
    source_paths = {_path(track.path) for album in inventory.albums for track in album.tracks}
    if _path(str(target.absolute())) in source_paths:
        raise ValueError("L'export ne peut pas remplacer un fichier de la médiathèque.")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def import_session(path: str | Path, path_mappings=None) -> Session:
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_SESSION_BYTES + 1)
        if len(raw) > MAX_SESSION_BYTES:
            raise ValueError("Session trop volumineuse.")
        def reject_constant(value):
            raise ValueError(f"Constante JSON non autorisée : {value}")
        payload = _validate(json.loads(raw.decode("utf-8-sig"), parse_constant=reject_constant))
    except (UnicodeError, json.JSONDecodeError, RecursionError, TypeError, KeyError) as error:
        raise ValueError("Fichier de session invalide.") from error
    current_root = payload["inventory"]["root"]
    provenance = payload.get("provenance", {})
    original_root = provenance.get("original_root", current_root)
    mappings = []
    for value in path_mappings or []:
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError("Correspondance de chemins invalide.")
        source, destination = value
        _path(source)
        _path(destination)
        if not _contained(source, current_root):
            raise ValueError("Une correspondance source est hors de la racine historique.")
        mappings.append((source, destination))

    def remap(value):
        parsed = _path(value)
        options = [(len(_path(source).parts), source, destination) for source, destination in mappings if _contained(value, source)]
        if not options:
            return value
        longest = max(option[0] for option in options)
        best = {(source, destination) for depth, source, destination in options if depth == longest}
        resolved = {str(_path(destination).joinpath(*parsed.relative_to(_path(source)).parts)) for source, destination in best}
        if len(resolved) != 1:
            raise ValueError("Correspondances de chemins ambiguës.")
        return next(iter(resolved))

    new_root = remap(current_root)
    if mappings and new_root == current_root and not any(_path(source) == _path(current_root) for source, _ in mappings):
        raise ValueError("Incluez une correspondance explicite pour la racine historique.")
    original_paths = {}
    mapped_paths = set()

    def remember(value):
        mapped = remap(value)
        original = provenance.get("original_paths", {}).get(value, value)
        if _path(mapped) in mapped_paths:
            raise ValueError("Plusieurs chemins historiques correspondent au même chemin local.")
        mapped_paths.add(_path(mapped))
        original_paths[mapped] = original
        if not _contained(mapped, new_root):
            raise ValueError("La correspondance place un chemin hors de la nouvelle racine.")
        return mapped

    data = payload["inventory"]
    data["root"] = new_root
    for album in data["albums"]:
        album["path"] = remember(album["path"])
        for track in album["tracks"]:
            track["path"] = remember(track["path"])
            if not _contained(track["path"], album["path"]):
                raise ValueError("Une correspondance déplace une piste hors de son album.")
            track["device"] = track["inode"] = None
    inventory = Inventory.from_dict(data)
    tracks = {_path(track.path): track for album in inventory.albums for track in album.tracks}
    for comparison in payload.get("comparisons", []):
        comparison["left_path"] = remap(comparison["left_path"])
        comparison["right_path"] = remap(comparison["right_path"])
        comparison["historical"] = True
    for decision in payload.get("decisions", []):
        decision["left_path"] = remap(decision["left_path"])
        decision["right_path"] = remap(decision["right_path"])
        decision["stale"] = True
    groups = [[tracks[_path(remap(value))] for value in group] for group in payload.get("exact_groups", [])]
    return Session(inventory, payload.get("comparisons", []), groups, payload.get("decisions", []),
                   payload["exported_at"], payload["rules_version"], original_root=original_root,
                   original_paths=original_paths,
                   path_mappings=[tuple(mapping) for mapping in provenance.get("path_mappings", [])] + mappings,
                   app_version=payload["app_version"])


save_session = export_session
load_session = import_session
