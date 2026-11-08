"""Empreintes locales Chromaprint et recherche AcoustID explicite.

Un résultat AcoustID identifie potentiellement un enregistrement, jamais une
édition. Aucune métadonnée de la médiathèque n'est modifiée.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .cache import check_cancel
from .models import Track
from .musicbrainz import ServiceUnavailable, USER_AGENT, UUID


class FeatureUnavailable(RuntimeError):
    """Un composant local facultatif doit être installé."""


@dataclass(frozen=True)
class Fingerprint:
    duration: float
    fingerprint: str


@dataclass(frozen=True)
class RecordingCandidate:
    id: str
    title: str
    artist: str
    score: float
    url: str


def _version(track: Track):
    path = Path(track.path)
    if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
        raise OSError("Lien ignoré lors de la reconnaissance audio.")
    info = path.stat(follow_symlinks=False)
    if (info.st_size, info.st_mtime_ns) != (track.size, track.mtime_ns):
        raise OSError("Fichier modifié depuis le scan ; relancez l'analyse.")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _valid_fingerprint(value: dict) -> Fingerprint:
    try:
        duration = float(value["duration"])
        fingerprint = value["fingerprint"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Empreinte Chromaprint invalide.") from exc
    if (not math.isfinite(duration) or duration <= 0 or not isinstance(fingerprint, str)
            or not fingerprint or len(fingerprint) > 100000
            or not re.fullmatch(r"[A-Za-z0-9_=-]+", fingerprint)):
        raise ValueError("Empreinte Chromaprint invalide.")
    return Fingerprint(duration, fingerprint)


class Fingerprinter:
    def __init__(self, cache_path: str | Path, fpcalc: str = "fpcalc"):
        path = Path(cache_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS fingerprints (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, body TEXT)")
        self.db.commit()
        self.fpcalc = fpcalc
        self._executable = None

    def close(self):
        self.db.close()

    def _run(self, arguments, cancel=None):
        options = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True,
                   "encoding": "utf-8", "errors": "replace"}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        with subprocess.Popen(arguments, **options) as process:
            deadline = time.monotonic() + 90
            try:
                while True:
                    check_cancel(cancel)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise FeatureUnavailable("Chromaprint n'a pas terminé dans le délai de 90 secondes.")
                    try:
                        stdout, stderr = process.communicate(timeout=min(0.15, remaining))
                        break
                    except subprocess.TimeoutExpired:
                        continue
            except BaseException:
                process.kill()
                process.communicate()
                raise
        if process.returncode:
            raise FeatureUnavailable(f"Chromaprint a échoué (code {process.returncode}) : {stderr[-300:].strip()}")
        return stdout

    def _find_executable(self, cancel=None):
        if self._executable:
            return self._executable
        executable = shutil.which(self.fpcalc)
        if executable is None and self.fpcalc == "fpcalc":
            filename = "fpcalc.exe" if os.name == "nt" else "fpcalc"
            roots = [Path(sys.executable).parent, Path(getattr(sys, "_MEIPASS", ".")) / "bin"]
            executable = next((str(root / filename) for root in roots if (root / filename).is_file()), None)
        if executable is None:
            raise FeatureUnavailable("Chromaprint est absent : installez fpcalc ou indiquez son chemin. Le scan local reste disponible.")
        version = self._run([executable, "-version"], cancel)
        if not re.search(r"(?:fpcalc|chromaprint).*?\d+\.\d+", version, re.I):
            raise FeatureUnavailable("Impossible de vérifier la version de fpcalc.")
        self._executable = executable
        return executable

    def fingerprint(self, track: Track, cancel=None) -> Fingerprint:
        check_cancel(cancel)
        before = _version(track)
        row = self.db.execute("SELECT body FROM fingerprints WHERE path=? AND size=? AND mtime_ns=?",
                              (track.path, track.size, track.mtime_ns)).fetchone()
        if row:
            try:
                return _valid_fingerprint(json.loads(row[0]))
            except (ValueError, TypeError):
                pass
        executable = self._find_executable(cancel)
        try:
            value = json.loads(self._run([executable, "-json", "-length", "120", track.path], cancel))
            result = _valid_fingerprint(value)
        except (ValueError, TypeError) as exc:
            raise FeatureUnavailable("Réponse Chromaprint illisible.") from exc
        if _version(track) != before:
            raise OSError("Fichier modifié pendant le calcul de l'empreinte.")
        check_cancel(cancel)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO fingerprints VALUES (?,?,?,?)",
                            (track.path, track.size, track.mtime_ns, json.dumps(value)))
        return result


class _RequestQueue:
    def __init__(self):
        self.lock = threading.Lock()
        self.last = float("-inf")

    def request(self, request, opener, max_bytes):
        with self.lock:
            delay = 1 - (time.monotonic() - self.last)
            if delay > 0:
                time.sleep(delay)
            self.last = time.monotonic()
            with opener(request, timeout=20) as response:
                value = response.read(max_bytes + 1)
            if len(value) > max_bytes:
                raise ServiceUnavailable("Réponse distante trop volumineuse.")
            return value


_REQUESTS = _RequestQueue()


def _read_url(url, opener=urlopen, max_bytes=8 * 1024 * 1024):
    """GET borné avec TLS normal ; aucun message ne révèle les paramètres API."""
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, image/jpeg, image/png"})
    for attempt in range(3):
        try:
            return _REQUESTS.request(request, opener, max_bytes)
        except HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise ServiceUnavailable(f"Service distant indisponible (HTTP {exc.code}).") from exc
            retry = exc.headers.get("Retry-After", "") if exc.headers else ""
            try:
                delay = float(retry)
            except (ValueError, TypeError):
                try:
                    delay = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    delay = 2 ** attempt
            if not math.isfinite(delay) or delay > 10:
                raise ServiceUnavailable("Le service demande de réessayer plus tard.") from exc
            time.sleep(max(1, delay))
        except (URLError, TimeoutError, OSError) as exc:
            if attempt == 2:
                raise ServiceUnavailable("Service distant inaccessible ou délai dépassé.") from exc
            time.sleep(2 ** attempt)
    raise ServiceUnavailable("Service distant indisponible.")


class AcoustIDClient:
    """La clé est gardée en mémoire uniquement ; le cache ne contient pas la clé."""
    def __init__(self, api_key: str, cache_path: str | Path | None = None, opener=urlopen):
        if not api_key or not api_key.strip():
            raise ValueError("Une clé d'application AcoustID est nécessaire.")
        self.api_key, self.opener = api_key.strip(), opener
        self.db = None
        if cache_path is not None:
            path = Path(cache_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path)
            self.db.execute("CREATE TABLE IF NOT EXISTS acoustid (key TEXT PRIMARY KEY, fetched REAL, body TEXT)")
            self.db.commit()

    def close(self):
        if self.db:
            self.db.close()

    def lookup(self, fingerprint: Fingerprint) -> list[RecordingCandidate]:
        fingerprint = _valid_fingerprint({"duration": fingerprint.duration, "fingerprint": fingerprint.fingerprint})
        key = hashlib.sha256(f"{int(fingerprint.duration)}:{fingerprint.fingerprint}".encode()).hexdigest()
        response = None
        if self.db:
            row = self.db.execute("SELECT fetched,body FROM acoustid WHERE key=?", (key,)).fetchone()
            if row and 0 <= time.time() - row[0] < 7 * 86400:
                try:
                    response = json.loads(row[1])
                except ValueError:
                    pass
        if response is None:
            url = "https://api.acoustid.org/v2/lookup?" + urlencode({
                "client": self.api_key, "duration": int(fingerprint.duration),
                "fingerprint": fingerprint.fingerprint, "meta": "recordings", "format": "json"})
            body = _read_url(url, self.opener)
            try:
                response = json.loads(body) if body else None
            except (ValueError, TypeError) as exc:
                raise ServiceUnavailable("Réponse AcoustID illisible.") from exc
        if not isinstance(response, dict) or response.get("status") != "ok" or not isinstance(response.get("results"), list):
            raise ServiceUnavailable("AcoustID a refusé la requête ou retourné une réponse invalide.")
        candidates = {}
        try:
            for item in response["results"]:
                score = float(item.get("score", 0))
                if not math.isfinite(score) or not 0 <= score <= 1:
                    raise ValueError("Score invalide")
                for recording in item.get("recordings", []):
                    mbid = recording["id"]
                    if not isinstance(mbid, str) or not UUID.fullmatch(mbid):
                        raise ValueError("MBID invalide")
                    title = recording.get("title", "")
                    artists = recording.get("artists", [])
                    if not isinstance(title, str) or not isinstance(artists, list):
                        raise ValueError("Crédit invalide")
                    names = [artist.get("name", "") for artist in artists]
                    if not all(isinstance(name, str) for name in names):
                        raise ValueError("Crédit invalide")
                    candidate = RecordingCandidate(mbid, title, "; ".join(names), score,
                                                   "https://musicbrainz.org/recording/" + mbid)
                    if mbid not in candidates or candidates[mbid].score < score:
                        candidates[mbid] = candidate
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise ServiceUnavailable("Résultats AcoustID illisibles.") from exc
        if self.db:
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO acoustid VALUES (?,?,?)", (key, time.time(), json.dumps(response)))
        return sorted(candidates.values(), key=lambda item: (-item.score, item.id))
