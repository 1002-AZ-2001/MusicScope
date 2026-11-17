"""Faux sans-perte : FLAC ou ALAC fabriqués à partir d'un MP3 ou d'un AAC.

Un encodeur avec perte coupe les aigus au-dessus d'une fréquence fixe (environ 16-17 kHz
à 128 kb/s, 19-20 kHz à 320 kb/s) ; reconverti en FLAC, le fichier garde cette coupure
nette, puis du silence. Un vrai enregistrement sans perte perd ses aigus progressivement.

MusicScope mesure, avec FFmpeg, l'énergie de neuf bandes d'1 kHz entre 12 et 21 kHz sur
30 secondes du morceau. Une chute d'au moins 20 dB entre deux bandes voisines jusqu'au
plancher numérique signale une coupure. C'est un indice, pas une preuve : un master ancien
ou volontairement filtré peut aussi en avoir une. Rien n'est jamais modifié.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import time

from .cache import check_cancel
from .models import Album, Inventory, Track

CENTERS = tuple(range(12500, 21500, 1000))  # bandes d'1 kHz, de 12 à 21 kHz
FLOOR_DB = -85.0         # silence numérique (volumedetect plafonne vers -91 dB en 16 bits)
CLIFF_DB = 20.0          # chute minimale entre deux bandes voisines pour parler de coupure
QUIET_DB = -80.0         # bande de référence trop faible : aucune conclusion possible
LOSSLESS = {"FLAC", "ALAC"}

GENUINE = "Spectre complet"
SUSPECT = "Suspect"
REVIEW = "À vérifier"
UNKNOWN = "Indéterminé"
VERDICT_ORDER = {SUSPECT: 0, REVIEW: 1, UNKNOWN: 2, GENUINE: 3}


class SpectrumUnavailable(RuntimeError):
    pass


@dataclass
class SpectrumResult:
    verdict: str
    reason: str
    cutoff_khz: float | None = None
    bands: list[float] = field(default_factory=list)

    @property
    def doubtful(self) -> bool:
        return self.verdict in (SUSPECT, REVIEW)


def interpret(bands: list[float], sample_rate: int | None) -> SpectrumResult:
    """Verdict à partir des niveaux moyens (dB) des bandes CENTERS."""
    if sample_rate and sample_rate < 44100:
        return SpectrumResult(UNKNOWN, f"Échantillonnage à {sample_rate} Hz : les aigus au-delà de "
                                       f"{sample_rate / 2000:.0f} kHz n'existent pas, aucune conclusion.", None, bands)
    if len(bands) != len(CENTERS):
        return SpectrumResult(UNKNOWN, "Mesure incomplète.", None, bands)
    if bands[0] < QUIET_DB:
        return SpectrumResult(UNKNOWN, "Passage trop calme dans les aigus pour juger.", None, bands)
    for index in range(1, len(bands)):
        above, below = bands[index - 1], bands[index]
        if below <= FLOOR_DB and above - below >= CLIFF_DB and above > FLOOR_DB + 10:
            cutoff = (CENTERS[index - 1] + CENTERS[index]) / 2000
            if cutoff <= 17.5:
                return SpectrumResult(SUSPECT, f"Coupure nette vers {cutoff:.0f} kHz puis silence : signature d'un "
                                               "MP3 ou AAC de débit moyen (≈ 128-192 kb/s) reconverti en sans-perte.",
                                      cutoff, bands)
            return SpectrumResult(REVIEW, f"Coupure nette vers {cutoff:.0f} kHz : possible MP3 ou AAC de haut débit "
                                          "reconverti, ou master filtré. À comparer avec une autre source.",
                                  cutoff, bands)
    return SpectrumResult(GENUINE, "Aigus présents jusqu'à 21 kHz ou atténuation progressive : pas de coupure "
                                   "d'encodeur avec perte.", None, bands)


def _find_ffmpeg(name: str = "ffmpeg") -> str:
    executable = shutil.which(name)
    if executable is None:
        raise SpectrumUnavailable("FFmpeg est introuvable : installez-le (paquet « ffmpeg ») pour analyser le spectre.")
    return executable


def measure(path: str, duration: float | None, ffmpeg: str, cancel=None, timeout: float = 120) -> list[float]:
    """Niveaux moyens des bandes, sur 30 s prises au milieu du morceau."""
    start = max(0.0, min(30.0, (duration or 0) / 2 - 15))
    chains = ";".join(f"[s{index}]bandpass=f={center}:width_type=h:w=900,bandpass=f={center}:width_type=h:w=900,"
                      f"volumedetect[o{index}]" for index, center in enumerate(CENTERS))
    graph = (f"[0:a:0]aformat=channel_layouts=mono,asplit={len(CENTERS)}"
             + "".join(f"[s{index}]" for index in range(len(CENTERS))) + ";" + chains)
    command = [ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-ss", f"{start:.1f}", "-t", "30",
               "-i", os.path.abspath(path), "-filter_complex", graph]
    for index in range(len(CENTERS)):
        command += ["-map", f"[o{index}]", "-f", "null", "-"]
    options = {"stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE, "text": True,
               "encoding": "utf-8", "errors": "replace"}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    with subprocess.Popen(command, **options) as process:
        deadline = time.monotonic() + timeout
        while process.poll() is None:
            check_cancel_or_kill(process, cancel)
            if time.monotonic() > deadline:
                process.kill()
                raise SpectrumUnavailable("FFmpeg ne répond pas (délai dépassé).")
            time.sleep(0.02)
        output = process.stderr.read()
    found = sorted((int(number), float(level)) for number, level in
                   re.findall(r"Parsed_volumedetect_(\d+) @ \S+\] mean_volume: (-?[\d.]+|-inf) dB", output.replace("-inf", "-200")))
    if len(found) != len(CENTERS):
        raise SpectrumUnavailable("Mesure FFmpeg illisible : fichier non décodable ?")
    return [level for _, level in found]


def check_cancel_or_kill(process, cancel) -> None:
    try:
        check_cancel(cancel)
    except BaseException:
        process.kill()
        raise


class SpectrumCache:
    """Résultats par (chemin, taille, date) : un fichier inchangé n'est jamais réanalysé."""

    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("CREATE TABLE IF NOT EXISTS spectrum (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, "
                        "body TEXT)")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> SpectrumCache:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def get(self, track: Track) -> SpectrumResult | None:
        row = self.db.execute("SELECT body FROM spectrum WHERE path=? AND size=? AND mtime_ns=?",
                              (track.path, track.size, track.mtime_ns)).fetchone()
        if not row:
            return None
        try:
            return SpectrumResult(**json.loads(row[0]))
        except (ValueError, TypeError):
            return None

    def put(self, track: Track, result: SpectrumResult) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO spectrum VALUES (?,?,?,?)",
                            (track.path, track.size, track.mtime_ns, json.dumps(asdict(result))))

    def known(self, inventory: Inventory) -> dict[str, SpectrumResult]:
        """Résultats encore valides pour les pistes de l'inventaire (lecture du cache seulement)."""
        result = {}
        for album in inventory.albums:
            for track in album.tracks:
                if track.codec in LOSSLESS:
                    value = self.get(track)
                    if value is not None:
                        result[track.path] = value
        return result


def sample_tracks(album: Album, per_album: int | None) -> list[Track]:
    """Pistes sans perte à analyser ; `per_album` prend les plus centrales (moins de silence)."""
    tracks = [track for track in album.ordered_tracks if track.codec in LOSSLESS]
    if per_album is None or len(tracks) <= per_album:
        return tracks
    middle = len(tracks) // 2
    return sorted(tracks[max(0, middle - per_album // 2):][:per_album], key=lambda track: track.path)


def analyze(albums: list[Album], cache: SpectrumCache, per_album: int | None = 1, cancel=None,
            progress=None, ffmpeg: str | None = None, sample_rates: dict[str, int] | None = None
            ) -> dict[str, SpectrumResult]:
    executable = ffmpeg or _find_ffmpeg()
    jobs = [track for album in albums for track in sample_tracks(album, per_album)]
    results = {}
    for position, track in enumerate(jobs, 1):
        check_cancel(cancel)
        cached = cache.get(track)
        if cached is None:
            try:
                rate = (sample_rates or {}).get(track.path) or _sample_rate(track.path)
                cached = interpret(measure(track.path, track.duration, executable, cancel), rate)
            except (OSError, SpectrumUnavailable) as exc:
                cached = SpectrumResult(UNKNOWN, f"Analyse impossible : {exc}")
            if cached.verdict != UNKNOWN or "impossible" not in cached.reason:
                cache.put(track, cached)
        results[track.path] = cached
        if progress:
            progress(position, len(jobs), f"Spectre : {track.path}")
    return results


def _sample_rate(path: str) -> int | None:
    try:
        import mutagen
        audio = mutagen.File(path)
        return int(getattr(audio.info, "sample_rate", 0)) or None
    except Exception:
        return None


def album_verdict(album: Album, spectra: dict[str, SpectrumResult]) -> SpectrumResult | None:
    """Pire verdict des pistes analysées de l'album."""
    known = [spectra[track.path] for track in album.tracks if track.path in spectra]
    if not known:
        return None
    return min(known, key=lambda result: VERDICT_ORDER[result.verdict])
