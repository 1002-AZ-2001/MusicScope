"""Décisions personnelles et rapports de coût, sans action sur la médiathèque."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat

from .audit import audit_album
from .identity import MountCache, file_identity
from .models import Album, Inventory, Track

RULES_VERSION = "musicscope-review-1"
DECISIONS = ("Conserver les deux", "À revoir", "Faux positif")


def album_signature(album: Album) -> str:
    body = asdict(album)
    body["tracks"] = sorted(body["tracks"], key=lambda track: track["path"])
    encoded = json.dumps({"rules": RULES_VERSION, "album": body}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _current_stat(track: Track):
    try:
        path = Path(track.path)
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            return None
        current = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode) or (current.st_size, current.st_mtime_ns) != (track.size, track.mtime_ns):
            return None
        return current
    except OSError:
        return None


def _album_current(album: Album) -> bool:
    return bool(album.tracks and all(_current_stat(track) is not None for track in album.tracks))


@dataclass
class Decision:
    left_path: str
    right_path: str
    decision: str
    note: str
    recorded_at: str
    stale: bool
    left_signature: str
    right_signature: str
    rules_version: str = RULES_VERSION


class ReviewStore:
    """Une connexion par fil ; les décisions restent liées aux preuves datées."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=15)
        self.db.execute("""CREATE TABLE IF NOT EXISTS review_decisions (
            left_path TEXT, right_path TEXT, body TEXT NOT NULL,
            PRIMARY KEY (left_path,right_path))""")
        self.db.commit()

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @staticmethod
    def _ordered(left: Album, right: Album) -> tuple[Album, Album]:
        if left.path == right.path:
            raise ValueError("Choisissez deux dossiers distincts pour une décision.")
        return tuple(sorted((left, right), key=lambda album: album.path))

    def record(self, left: Album, right: Album, decision: str, note: str = "") -> Decision:
        if decision not in DECISIONS:
            raise ValueError("Décision personnelle inconnue.")
        if not isinstance(note, str) or len(note) > 10000:
            raise ValueError("La note doit contenir au plus 10 000 caractères.")
        left, right = self._ordered(left, right)
        result = Decision(left.path, right.path, decision, note,
                          datetime.now(timezone.utc).isoformat(),
                          not (_album_current(left) and _album_current(right)),
                          album_signature(left), album_signature(right))
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO review_decisions VALUES (?,?,?)",
                            (left.path, right.path, json.dumps(asdict(result), ensure_ascii=False)))
        return result

    @staticmethod
    def _refresh(body: str, albums: dict[str, Album]) -> Decision:
        record = Decision(**json.loads(body))
        left, right = albums.get(record.left_path), albums.get(record.right_path)
        record.stale = (record.rules_version != RULES_VERSION or left is None or right is None
                        or record.left_signature != album_signature(left)
                        or record.right_signature != album_signature(right)
                        or not _album_current(left) or not _album_current(right))
        return record

    def get(self, left: Album, right: Album) -> Decision | None:
        left, right = self._ordered(left, right)
        row = self.db.execute("SELECT body FROM review_decisions WHERE left_path=? AND right_path=?",
                              (left.path, right.path)).fetchone()
        return self._refresh(row[0], {left.path: left, right.path: right}) if row else None

    def list(self, inventory: Inventory) -> list[Decision]:
        albums = {album.path: album for album in inventory.albums}
        result = []
        for left, right, body in self.db.execute(
                "SELECT left_path,right_path,body FROM review_decisions ORDER BY left_path,right_path"):
            root = Path(inventory.root).absolute()
            if (inventory.root and Path(left).absolute().is_relative_to(root)
                    and Path(right).absolute().is_relative_to(root)):
                result.append(self._refresh(body, albums))
        return result


@dataclass
class QualityFinding:
    priority: int
    category: str
    album_path: str
    message: str


def quality_findings(inventory: Inventory) -> list[QualityFinding]:
    result = []
    seen = set()
    album_issues = {issue for album in inventory.albums for issue in album.issues}
    for error in inventory.errors:
        if error not in album_issues:
            result.append(QualityFinding(0, "Lecture", inventory.root, error))
    for album in inventory.albums:
        issues = list(dict.fromkeys(album.issues + audit_album(album)))
        for issue in issues:
            text = issue.casefold()
            if "illisible" in text or "lecture impossible" in text:
                priority, category = 0, "Lecture"
            elif "incomplet" in text or "aucune piste lisible" in text:
                priority, category = 1, "Inventaire incomplet"
            elif "incohérent" in text or "ambigu" in text or "contredit" in text:
                priority, category = 2, "Incohérence"
            elif any(word in text for word in ("disque", "numéro", "pistes potentiellement", "numérotation")):
                priority, category = 3, "Numérotation"
            else:
                priority, category = 4, "Tags manquants" if "manquant" in text else "Métadonnées"
            key = album.path, issue
            if key not in seen:
                seen.add(key)
                result.append(QualityFinding(priority, category, album.path, issue))
    return sorted(result, key=lambda finding: (finding.priority, finding.album_path, finding.message))


@dataclass
class StorageSummary:
    apparent_bytes: int
    known_unique_bytes: int | None
    verified_duplicate_bytes: int | None
    reasons: list[str] = field(default_factory=list)


def _tracks(inventory: Inventory) -> dict[str, Track]:
    return {track.path: track for album in inventory.albums for track in album.tracks}


def storage_summary(inventory: Inventory, exact_groups=()) -> StorageSummary:
    tracks = _tracks(inventory)
    result = StorageSummary(sum(track.size for track in tracks.values()), None, None)
    identities, info = {}, {}
    mounts = MountCache()
    for track in tracks.values():
        if track.device is None or track.inode is None:
            result.reasons.append("Identité physique indisponible ou inventaire périmé : volume physique et récupération indéterminés.")
            return result
        current = _current_stat(track)
        if current is None:
            result.reasons.append("Identité physique indisponible ou inventaire périmé : volume physique et récupération indéterminés.")
            return result
        identity = file_identity(Path(track.path), current, mounts)
        if identity != (track.device, track.inode):
            result.reasons.append("Identité physique du volume non vérifiable : volume physique et récupération indéterminés.")
            return result
        identities[track.path] = identity
        info[identity] = current
    result.known_unique_bytes = sum(current.st_size for current in info.values())
    seen = set()
    duplicate_bytes = 0
    counts = Counter(identities.values())
    for group in exact_groups:
        members = [track for track in group if track.path in tracks]
        if len(members) != len(group) or len({track.size for track in members}) > 1:
            result.reasons.append("Un groupe SHA-256 ne correspond pas à cet inventaire ; récupération indéterminée.")
            return result
        if any((track.size, track.mtime_ns) != (tracks[track.path].size, tracks[track.path].mtime_ns) for track in members):
            result.reasons.append("Un groupe SHA-256 est ancien ; récupération indéterminée.")
            return result
        physical = {identities[track.path] for track in members}
        if physical & seen:
            result.reasons.append("Groupes SHA-256 chevauchants : récupération indéterminée.")
            return result
        seen.update(physical)
        if any(info[identity].st_nlink > counts[identity] for identity in physical):
            result.reasons.append("Des liens physiques existent hors de l'inventaire ; l'espace récupérable est indéterminé.")
            return result
        if len(physical) > 1:
            duplicate_bytes += (len(physical) - 1) * members[0].size
    result.verified_duplicate_bytes = duplicate_bytes
    result.reasons.append("Chaque identité locale vérifiée est comptée une fois. Les copies SHA-256 distinctes donnent une borne supérieure, jamais une recommandation de suppression.")
    return result


@dataclass
class HashingCost:
    candidate_files: int
    candidate_bytes: int
    uncached_bytes: int
    stale_files: int = 0


def hashing_cost(inventory: Inventory, cache) -> HashingCost:
    groups = defaultdict(list)
    for track in _tracks(inventory).values():
        groups[track.size].append(track)
    candidates = [track for group in groups.values() if len(group) > 1 for track in group]
    result = HashingCost(len(candidates), sum(track.size for track in candidates), 0)
    for track in candidates:
        current = _current_stat(track)
        if current is None:
            result.stale_files += 1
        cached = current is not None and cache.connection.execute(
            "SELECT 1 FROM hashes WHERE path=? AND size=? AND mtime_ns=?",
            (track.path, track.size, track.mtime_ns)).fetchone()
        if not cached:
            result.uncached_bytes += track.size
    return result
