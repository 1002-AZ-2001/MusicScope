"""Écriture de tags confirmée, journalisée et annulable.

C'est la seule partie de MusicScope qui écrit dans les fichiers audio. Chaque
écriture suit le même ordre :

1. le fichier doit avoir la taille et la date relevées par le dernier scan ;
2. les valeurs d'origine sont enregistrées dans le journal SQLite (validé) ;
3. les tags sont écrits, puis relus et comparés aux valeurs demandées ;
4. en cas d'écart, les valeurs d'origine sont réécrites.

L'annulation réécrit les valeurs d'origine, seulement si le fichier porte
encore les valeurs écrites par MusicScope.
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Callable

import mutagen
from mutagen.easyid3 import EasyID3
from mutagen.id3 import ID3, ID3NoHeaderError

from .cache import check_cancel
from .models import Album

# Champs modifiables, identiques pour FLAC, Ogg, Opus (Vorbis), MP3 (ID3) et M4A (MP4).
EDITABLE_FIELDS = ("albumartist", "album", "date", "genre", "discnumber", "tracknumber", "title", "artist")
FIELD_LABELS = {
    "albumartist": "Artiste d’album", "album": "Album", "date": "Date", "genre": "Genre",
    "discnumber": "Disque", "tracknumber": "N°", "title": "Titre", "artist": "Artiste",
}
SEPARATOR = ";"


def split_values(text: str) -> list[str]:
    """« A; B » devient ["A", "B"] ; un texte vide supprime le champ."""
    return [part.strip() for part in text.split(SEPARATOR) if part.strip()]


def join_values(values: list[str]) -> str:
    return f"{SEPARATOR} ".join(values)


@dataclass
class FileEdit:
    path: str
    size: int
    mtime_ns: int
    changes: dict[str, list[str]]
    current: dict[str, str] = field(default_factory=dict)


@dataclass
class EditResult:
    batch_id: int
    written: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class Batch:
    id: int
    created_at: str
    album_path: str
    files: int
    written: int
    undone_at: str | None


def plan_album_edits(album: Album, album_values: dict[str, str],
                     track_values: dict[str, dict[str, str]]) -> list[FileEdit]:
    """Différences entre les valeurs saisies et les tags de l'inventaire.

    `album_values` s'applique à toutes les pistes ; un champ absent du
    dictionnaire n'est pas modifié. `track_values` est indexé par chemin.
    Seuls les fichiers dont au moins un champ change sont retenus.
    """
    for key in (*album_values, *(key for values in track_values.values() for key in values)):
        if key not in EDITABLE_FIELDS:
            raise ValueError(f"Champ non modifiable : {key}")
    edits = []
    for track in album.ordered_tracks:
        wanted = {**album_values, **track_values.get(track.path, {})}
        changes, current = {}, {}
        for key, text in wanted.items():
            values = split_values(text)
            if join_values(values) != join_values(split_values(track.value(key))):
                changes[key] = values
                current[key] = track.value(key)
        if changes:
            edits.append(FileEdit(track.path, track.size, track.mtime_ns, changes, current))
    return edits


class _Tags:
    """Interface « easy » de Mutagen, avec conservation de la version ID3."""

    def __init__(self, path: Path):
        self.path = path
        self.id3_version = None
        audio = mutagen.File(path)
        if audio is None:
            raise ValueError("Format audio non reconnu")
        if type(audio).__name__ == "MP3":
            try:
                self.id3_version = ID3(path).version[1]
                self.tags = EasyID3(path)
            except ID3NoHeaderError:
                self.id3_version = 4
                self.tags = EasyID3()
        else:
            audio = mutagen.File(path, easy=True)
            if audio.tags is None:
                audio.add_tags()
            self.tags = audio
        self.kind = type(audio).__name__

    def get(self, key: str) -> list[str]:
        try:
            return [str(value) for value in self.tags.get(key, [])]
        except (KeyError, ValueError):
            return []

    def normalized(self, key: str, values: list[str]) -> list[str]:
        """Valeurs telles que ce format les relira (ID3v2.3 n'a qu'un texte par cadre)."""
        if self.id3_version == 3 and len(values) > 1:
            return [join_values(values)]
        return list(values)

    def set(self, key: str, values: list[str]) -> None:
        values = self.normalized(key, values)
        if values:
            self.tags[key] = values
        elif key in self.tags:
            del self.tags[key]

    def save(self) -> None:
        if self.id3_version is not None:
            self.tags.save(self.path, v2_version=3 if self.id3_version == 3 else 4)
        else:
            self.tags.save()


def _write(path: Path, values: dict[str, list[str]]) -> None:
    """Écrit puis relit ; lève ValueError si le fichier ne porte pas les valeurs demandées."""
    tags = _Tags(path)
    for key, new in values.items():
        tags.set(key, new)
    tags.save()
    check = _Tags(path)
    for key, new in values.items():
        if check.get(key) != tags.normalized(key, new):
            raise ValueError(f"Relecture différente pour {key} : {check.get(key)!r}")


class TagJournal:
    """Journal SQLite des écritures ; une instance par thread."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS tag_batches (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, album_path TEXT NOT NULL,
                undone_at TEXT
            );
            CREATE TABLE IF NOT EXISTS tag_entries (
                batch_id INTEGER NOT NULL REFERENCES tag_batches(id), path TEXT NOT NULL,
                old_json TEXT NOT NULL, new_json TEXT NOT NULL, status TEXT NOT NULL,
                error TEXT NOT NULL DEFAULT '', PRIMARY KEY (batch_id, path)
            );
        """)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> TagJournal:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def apply(self, edits: list[FileEdit], album_path: str, cancel: Event | None = None,
              progress: Callable[[int, int, str], None] | None = None) -> EditResult:
        if not edits:
            raise ValueError("Aucune modification à écrire")
        with self.db:
            batch_id = self.db.execute("INSERT INTO tag_batches (created_at, album_path) VALUES (?, ?)",
                                       (self._now(), album_path)).lastrowid
        result = EditResult(batch_id)
        for index, edit in enumerate(edits, 1):
            check_cancel(cancel)
            path = Path(edit.path)
            try:
                if path.is_symlink():
                    raise ValueError("Lien ignoré")
                stat = path.stat()
                if (stat.st_size, stat.st_mtime_ns) != (edit.size, edit.mtime_ns):
                    raise ValueError("Fichier modifié depuis l’analyse ; relancez l’analyse")
                tags = _Tags(path)
                old = {key: tags.get(key) for key in edit.changes}
            except (OSError, ValueError, mutagen.MutagenError) as exc:
                result.failed.append((edit.path, str(exc)))
                continue
            # Les valeurs d'origine sont validées dans le journal avant toute écriture.
            with self.db:
                self.db.execute("INSERT INTO tag_entries VALUES (?, ?, ?, ?, 'pending', '')",
                                (batch_id, edit.path, json.dumps(old), json.dumps(edit.changes)))
            try:
                _write(path, edit.changes)
            except (OSError, ValueError, mutagen.MutagenError) as exc:
                error = str(exc)
                try:
                    _write(path, old)
                    status = "failed_restored"
                except (OSError, ValueError, mutagen.MutagenError) as restore_error:
                    status = "failed"
                    error += f" ; restauration impossible : {restore_error}"
                with self.db:
                    self.db.execute("UPDATE tag_entries SET status=?, error=? WHERE batch_id=? AND path=?",
                                    (status, error, batch_id, edit.path))
                result.failed.append((edit.path, error))
            else:
                with self.db:
                    self.db.execute("UPDATE tag_entries SET status='written' WHERE batch_id=? AND path=?",
                                    (batch_id, edit.path))
                result.written.append(edit.path)
            if progress:
                progress(index, len(edits), edit.path)
        return result

    def batches(self) -> list[Batch]:
        rows = self.db.execute("""
            SELECT b.id, b.created_at, b.album_path, COUNT(e.path),
                   SUM(e.status = 'written'), b.undone_at
            FROM tag_batches b LEFT JOIN tag_entries e ON e.batch_id = b.id
            GROUP BY b.id ORDER BY b.id DESC""").fetchall()
        return [Batch(row[0], row[1], row[2], row[3], row[4] or 0, row[5]) for row in rows]

    def entries(self, batch_id: int) -> list[tuple[str, dict, dict, str]]:
        return [(path, json.loads(old), json.loads(new), status) for path, old, new, status in self.db.execute(
            "SELECT path, old_json, new_json, status FROM tag_entries WHERE batch_id=? ORDER BY path", (batch_id,))]

    def undo(self, batch_id: int, cancel: Event | None = None,
             progress: Callable[[int, int, str], None] | None = None) -> EditResult:
        """Réécrit les valeurs d'origine des fichiers qui portent encore les valeurs écrites."""
        row = self.db.execute("SELECT undone_at FROM tag_batches WHERE id=?", (batch_id,)).fetchone()
        if row is None:
            raise ValueError("Modification inconnue")
        if row[0]:
            raise ValueError("Cette modification a déjà été annulée")
        result = EditResult(batch_id)
        entries = [entry for entry in self.entries(batch_id) if entry[3] in {"written", "pending"}]
        for index, (path_text, old, new, status) in enumerate(entries, 1):
            check_cancel(cancel)
            path = Path(path_text)
            try:
                if path.is_symlink():
                    raise ValueError("Lien ignoré")
                tags = _Tags(path)
                current = {key: tags.get(key) for key in new}
                if current == {key: tags.normalized(key, values) for key, values in old.items()}:
                    pass  # écriture interrompue avant modification : rien à restaurer
                elif current != {key: tags.normalized(key, values) for key, values in new.items()}:
                    raise ValueError("Tags modifiés depuis l’écriture ; annulation refusée pour ce fichier")
                else:
                    _write(path, old)
            except (OSError, ValueError, mutagen.MutagenError) as exc:
                result.failed.append((path_text, str(exc)))
            else:
                with self.db:
                    self.db.execute("UPDATE tag_entries SET status='undone' WHERE batch_id=? AND path=?",
                                    (batch_id, path_text))
                result.written.append(path_text)
            if progress:
                progress(index, len(entries), path_text)
        if not result.failed:
            with self.db:
                self.db.execute("UPDATE tag_batches SET undone_at=? WHERE id=?", (self._now(), batch_id))
        return result


def proposals_to_values(proposals, album: Album) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    """Propositions MusicBrainz limitées aux champs modifiables.

    Un champ d'album n'est repris que s'il reçoit la même proposition sur toutes
    les pistes concernées ; sinon il est proposé piste par piste.
    """
    per_track: dict[str, dict[str, str]] = {}
    for proposal in proposals:
        if proposal.field in EDITABLE_FIELDS:
            per_track.setdefault(proposal.path, {})[proposal.field] = proposal.proposed
    album_values = {}
    paths = {track.path for track in album.tracks}
    for key in ("albumartist", "album", "date", "genre"):
        proposed = {values[key] for path, values in per_track.items() if key in values and path in paths}
        if len(proposed) == 1 and all(key in per_track.get(path, {}) or track_value == next(iter(proposed))
                                      for path, track_value in ((t.path, t.value(key)) for t in album.tracks)):
            album_values[key] = proposed.pop()
            for values in per_track.values():
                values.pop(key, None)
    return album_values, {path: values for path, values in per_track.items() if values and path in paths}


def describe(edits: list[FileEdit]) -> list[tuple[str, str, str, str]]:
    """Lignes d'aperçu : fichier, champ, avant, après."""
    return [(os.path.basename(edit.path), FIELD_LABELS.get(key, key), edit.current.get(key, ""), join_values(values))
            for edit in edits for key, values in edit.changes.items()]
