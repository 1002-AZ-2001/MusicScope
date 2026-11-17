"""Écriture de tags confirmée, journalisée et annulable.

C'est la seule partie de MusicScope qui écrit dans les fichiers audio. Chaque
écriture suit le même ordre :

1. le fichier doit avoir la taille et la date relevées par le dernier scan ;
2. les valeurs d'origine sont enregistrées dans le journal SQLite (validé) ;
3. les tags sont écrits sur une copie du fichier, dans le même dossier, puis relus et
   comparés aux valeurs demandées ;
4. seulement si tout concorde, la copie remplace l'original (renommage atomique) ;
   sinon l'original n'a jamais été touché.

Un fichier lié physiquement (qBittorrent, Lidarr) n'est jamais modifié : changer ses
tags modifierait aussi la copie partagée.

L'annulation réécrit les valeurs d'origine, seulement si le fichier porte
encore les valeurs écrites par MusicScope.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Callable
from uuid import uuid4

import mutagen

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
    cancelled: bool = False  # arrêt demandé : les fichiers déjà traités restent dans written/failed


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


# Cadres ID3v2.4 que mutagen retire en enregistrant au format v2.3 (update_to_v23) : tri
# (TSOP, TSOA, TSOT), dates (TDOR, TDRL), ReplayGain (RVA2)… Ils sont remis tels quels,
# comme le conseille la documentation de mutagen. TDRC (date éditable), TIPL et TMCL ont un
# équivalent v2.3 (TYER/TDAT, IPLS) et sont convertis normalement.
_V24_KEPT = ("ASPI", "EQU2", "RVA2", "SEEK", "SIGN", "TDEN", "TDOR", "TDRL", "TDTG", "TMOO", "TPRO",
             "TSOA", "TSOP", "TSOT", "TSST")
_MP4_NUMBERS = {"tracknumber", "discnumber"}


class _Tags:
    """Interface « easy » de Mutagen, avec conservation de la version ID3.

    `kind` (classe Mutagen) permet d'ouvrir une copie temporaire dont l'extension ne
    permet pas de deviner le format."""

    def __init__(self, path: Path, kind=None):
        self.path = Path(path)
        audio = kind(self.path) if kind is not None else mutagen.File(self.path, easy=True)
        if audio is None:
            raise ValueError("Format audio non reconnu")
        self.audio, self.kind_class = audio, type(audio)
        self.kind = type(audio).__name__.removeprefix("Easy")
        self.id3_version = None
        if audio.tags is None:
            audio.add_tags()
            if self.kind == "MP3":
                self.id3_version = 4
        elif self.kind == "MP3":
            self.id3_version = self._id3().version[1]
        self.tags = audio.tags

    def _id3(self):
        return getattr(self.audio.tags, "_EasyID3__id3", None)  # ID3 sous-jacent d'EasyID3

    def get(self, key: str) -> list[str]:
        try:
            return [str(value) for value in self.tags.get(key, [])]
        except (KeyError, ValueError):
            return []

    def normalized(self, key: str, values: list[str]) -> list[str]:
        """Valeurs telles que ce format les relira (ID3v2.3 n'a qu'un texte par cadre ;
        un numéro M4A garde le total déjà présent)."""
        if self.id3_version == 3 and len(values) > 1:
            return [join_values(values)]
        if self.kind == "MP4" and key in _MP4_NUMBERS and len(values) == 1 and "/" not in values[0]:
            current = self.get(key)
            if current and "/" in current[0]:
                total = current[0].split("/", 1)[1]
                if total.strip() not in {"", "0"}:
                    return [f"{values[0]}/{total}"]
        return list(values)

    def aliases(self, key: str) -> list[str]:
        """Autres noms Vorbis du même champ (ex. « ALBUM ARTIST ») que le scanner lit aussi."""
        from .metadata import ALIASES, _key
        if self.kind not in {"FLAC", "OggVorbis", "OggOpus", "OggFLAC", "OggSpeex"} or key not in ALIASES:
            return []
        # « album artist » et « albumartist » ont la même clé normalisée : comparer les noms bruts.
        wanted = {_key(alias) for alias in ALIASES[key]}
        return [name for name in list(self.tags.keys())
                if _key(name) in wanted and name.casefold() != key.casefold()]

    def snapshot(self, keys) -> dict[str, list[str]]:
        """Valeurs actuelles des champs, et de leurs variantes qui seront retirées."""
        values = {key: self.get(key) for key in keys}
        for key in keys:
            for alias in self.aliases(key):
                values.setdefault(alias, self.get(alias))
        return values

    def set(self, key: str, values: list[str]) -> list[str]:
        """Écrit le champ, retire ses variantes, et renvoie les valeurs attendues à la relecture."""
        values = self.normalized(key, values)
        for alias in self.aliases(key):
            del self.tags[alias]
        if values:
            self.tags[key] = values
        elif key in self.tags:
            del self.tags[key]
        return values

    def save(self, target: Path | None = None) -> None:
        target = Path(target or self.path)
        if self.id3_version is None:
            self.audio.save(target)
            return
        id3 = self._id3()
        if id3 is None:  # interne de mutagen inattendu : comportement d'origine
            self.tags.save(target, v2_version=3 if self.id3_version == 3 else 4)
            return
        if self.id3_version != 3:
            id3.save(target, v2_version=4)
            return
        import copy
        kept = [copy.deepcopy(frame) for name in _V24_KEPT for frame in id3.getall(name)]
        had_original_year = bool(id3.getall("TORY"))
        id3.update_to_v23()
        for frame in kept:
            id3.add(frame)
        if any(frame.FrameID == "TDOR" for frame in kept) and not had_original_year:
            id3.delall("TORY")  # TDOR complet remis : pas de doublon d'année ajouté
        # Séparateur nul : les valeurs multiples des champs non modifiés restent séparées.
        id3.save(target, v2_version=3, v23_sep=None)


def _temporary_copy(path: Path) -> Path:
    """Copie de travail dans le même dossier (même volume, donc remplacement atomique).
    Son extension n'est pas audio : une copie oubliée n'est jamais inventoriée."""
    temporary = path.with_name(f".{path.name}.{uuid4().hex[:12]}.musicscope-tmp")
    shutil.copy2(path, temporary)
    return temporary


def _write(path: Path, values: dict[str, list[str]], source: _Tags | None = None) -> None:
    """Écrit sur une copie, la relit, puis la substitue au fichier d'origine.

    Une coupure (réseau, alimentation) pendant l'écriture ne touche que la copie : le
    fichier d'origine reste intact jusqu'au remplacement final, qui est atomique.
    Lève ValueError si la copie ne porte pas les valeurs demandées."""
    path = Path(path)
    source = source or _Tags(path)
    before = path.stat()
    if before.st_nlink > 1:
        raise ValueError(LINKED_MESSAGE)
    temporary = _temporary_copy(path)
    try:
        tags = _Tags(temporary, source.kind_class)
        expected = {key: tags.set(key, new) for key, new in values.items()}
        tags.save()
        check = _Tags(temporary, source.kind_class)
        for key, new in expected.items():
            if check.get(key) != new:
                raise ValueError(f"Relecture différente pour {key} : {check.get(key)!r}")
            leftover = [alias for alias in check.aliases(key) if alias not in values]
            if leftover:
                raise ValueError(f"Variante de {key} restée en place : {leftover!r}")
        _same_audio(source.audio, check.audio)
        current = path.stat()
        if (current.st_size, current.st_mtime_ns, current.st_ino) != (before.st_size, before.st_mtime_ns, before.st_ino):
            raise ValueError("Fichier modifié pendant l’écriture ; rien n’a été remplacé")
        try:
            os.chown(temporary, before.st_uid, before.st_gid)
        except (OSError, AttributeError):
            pass  # propriétaire imposé par le montage (SMB) ou droits insuffisants
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _same_audio(before, after) -> None:
    """La partie audio relue doit avoir les mêmes caractéristiques que l'original."""
    left, right = before.info, after.info
    for name in ("sample_rate", "channels", "bits_per_sample"):
        if getattr(left, name, None) != getattr(right, name, None):
            raise ValueError(f"Caractéristique audio modifiée ({name}) ; rien n’a été remplacé")
    if abs((getattr(left, "length", 0) or 0) - (getattr(right, "length", 0) or 0)) > 0.05:
        raise ValueError("Durée audio modifiée ; rien n’a été remplacé")


LINKED_MESSAGE = ("Fichier lié physiquement (qBittorrent, Lidarr) : écriture refusée pour ne pas modifier la "
                  "copie partagée")


def linked_paths(edits: list[FileEdit]) -> list[str]:
    """Fichiers à plusieurs liens physiques : leurs tags ne sont jamais modifiés."""
    result = []
    for edit in edits:
        try:
            if os.stat(edit.path, follow_symlinks=False).st_nlink > 1:
                result.append(edit.path)
        except OSError:
            continue
    return result


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
            if cancel is not None and cancel.is_set():
                result.cancelled = True
                break
            path = Path(edit.path)
            try:
                if path.is_symlink():
                    raise ValueError("Lien ignoré")
                stat = path.stat()
                if (stat.st_size, stat.st_mtime_ns) != (edit.size, edit.mtime_ns):
                    raise ValueError("Fichier modifié depuis l’analyse ; relancez l’analyse")
                if stat.st_nlink > 1:
                    raise ValueError(LINKED_MESSAGE)
                tags = _Tags(path)
                old = tags.snapshot(edit.changes)
            except (OSError, ValueError, mutagen.MutagenError) as exc:
                result.failed.append((edit.path, str(exc)))
                continue
            # Les valeurs d'origine sont validées dans le journal avant toute écriture.
            with self.db:
                self.db.execute("INSERT INTO tag_entries VALUES (?, ?, ?, ?, 'pending', '')",
                                (batch_id, edit.path, json.dumps(old), json.dumps(edit.changes)))
            try:
                _write(path, edit.changes, tags)
            except (OSError, ValueError, mutagen.MutagenError) as exc:
                # L'écriture se fait sur une copie : le fichier d'origine n'a pas été remplacé.
                error, status = str(exc), "failed_restored"
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
        # « failed » : écriture puis restauration en échec, le fichier peut porter les nouvelles valeurs.
        entries = [entry for entry in self.entries(batch_id) if entry[3] in {"written", "pending", "failed"}]
        for index, (path_text, old, new, status) in enumerate(entries, 1):
            if cancel is not None and cancel.is_set():
                result.cancelled = True
                break
            path = Path(path_text)
            try:
                if path.is_symlink():
                    raise ValueError("Lien ignoré")
                tags = _Tags(path)
                current = {key: tags.get(key) for key in new}
                if current == {key: tags.normalized(key, old.get(key, [])) for key in new}:
                    pass  # écriture interrompue avant modification : rien à restaurer
                elif current != {key: tags.normalized(key, values) for key, values in new.items()}:
                    raise ValueError("Tags modifiés depuis l’écriture ; annulation refusée pour ce fichier")
                else:
                    _write(path, old, tags)
            except (OSError, ValueError, mutagen.MutagenError) as exc:
                result.failed.append((path_text, str(exc)))
            else:
                with self.db:
                    self.db.execute("UPDATE tag_entries SET status='undone' WHERE batch_id=? AND path=?",
                                    (batch_id, path_text))
                result.written.append(path_text)
            if progress:
                progress(index, len(entries), path_text)
        if not result.failed and not result.cancelled:
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
