"""Cache local SQLite. Aucun fichier de la médiathèque n'est ouvert en écriture."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import asdict
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import Iterator

from .models import Inventory, Track


class ScanCancelled(Exception):
    """Annulation volontaire ; le dernier inventaire reste intact."""


def check_cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise ScanCancelled("Analyse annulée")


class Cache:
    """Une instance par thread ; les transactions d'inventaire sont atomiques."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS tracks (
                path TEXT PRIMARY KEY, size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL, body TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS hashes (
                path TEXT PRIMARY KEY, size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL, sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS inventories (
                root TEXT PRIMARY KEY, body TEXT NOT NULL
            );
        """)
        self.connection.commit()
        self._deferred = 0
        self._pending = 0
        self._commit_every = 0

    @contextmanager
    def deferred_commits(self, every: int = 256) -> Iterator[None]:
        """Regroupe les écritures de pistes en transactions de `every` entrées.

        Les entrées déjà lues sont validées à la sortie, y compris après une
        annulation ou une erreur : un prochain scan peut les réutiliser.
        """
        self._deferred += 1
        previous, self._commit_every = self._commit_every, every
        try:
            yield
        finally:
            self._deferred -= 1
            self._commit_every = previous
            if not self._deferred:
                self._flush()

    def _flush(self) -> None:
        if self._pending:
            self.connection.commit()
            self._pending = 0

    def close(self) -> None:
        self._flush()
        self.connection.close()

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def get_track(self, path: str | Path, stat: os.stat_result) -> Track | None:
        row = self.connection.execute(
            "SELECT body FROM tracks WHERE path=? AND size=? AND mtime_ns=?",
            (str(path), stat.st_size, stat.st_mtime_ns),
        ).fetchone()
        if row:
            try:
                return Track(**json.loads(row[0]))
            except (ValueError, TypeError):
                return None
        return None

    def put_track(self, track: Track) -> None:
        if not self._deferred:
            with self.connection:
                self._insert_track(track)
            return
        try:
            self._insert_track(track)
        except sqlite3.Error:
            self.connection.rollback()
            self._pending = 0
            raise
        self._pending += 1
        if self._pending >= self._commit_every:
            self._flush()

    def _insert_track(self, track: Track) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO tracks VALUES (?, ?, ?, ?)",
            (track.path, track.size, track.mtime_ns, json.dumps(asdict(track))),
        )

    def save_inventory(self, inventory: Inventory) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO inventories VALUES (?, ?)",
                (inventory.root, json.dumps(inventory.to_dict(), ensure_ascii=False)),
            )

    def load_inventory(self, root: str | Path) -> Inventory | None:
        row = self.connection.execute(
            "SELECT body FROM inventories WHERE root=?", (str(Path(root).absolute()),)
        ).fetchone()
        return Inventory.from_dict(json.loads(row[0])) if row else None

    def hash_track(self, track: Track, cancel: Event | None = None) -> str:
        """SHA-256 en flux ; refuse un fichier modifié depuis l'inventaire."""
        check_cancel(cancel)
        path = Path(track.path)
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise OSError(f"Lien ignoré : {path}")
        before = path.stat(follow_symlinks=False)
        self._check_version(track, before)
        row = self.connection.execute(
            "SELECT sha256 FROM hashes WHERE path=? AND size=? AND mtime_ns=?",
            (track.path, track.size, track.mtime_ns),
        ).fetchone()
        if row:
            return row[0]
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            self._check_version(track, opened)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise OSError(f"Fichier remplacé pendant la lecture : {path}")
            digest = hashlib.sha256()
            while True:
                check_cancel(cancel)
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
            self._check_version(track, os.fstat(stream.fileno()))
        after = path.stat(follow_symlinks=False)
        self._check_version(track, after)
        if (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino):
            raise OSError(f"Fichier remplacé pendant la lecture : {path}")
        value = digest.hexdigest()
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO hashes VALUES (?, ?, ?, ?)",
                (track.path, track.size, track.mtime_ns, value),
            )
        return value

    @staticmethod
    def _check_version(track: Track, stat: os.stat_result) -> None:
        if (track.size, track.mtime_ns) != (stat.st_size, stat.st_mtime_ns):
            raise OSError(f"Fichier modifié depuis le scan : {track.path}")
