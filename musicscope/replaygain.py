"""ReplayGain 2.0 mesuré par FFmpeg (filtre ebur128, norme EBU R128).

Référence : -18 LUFS (ReplayGain 2.0, équivalent des 89 dB de ReplayGain 1). Les tags sont
REPLAYGAIN_TRACK_GAIN / _PEAK et REPLAYGAIN_ALBUM_GAIN / _PEAK, sauf en Opus : celui-ci
utilise R128_TRACK_GAIN et R128_ALBUM_GAIN, relatifs à -23 LUFS, en entier Q7.8 (1/256 dB,
RFC 7845). La crête est la crête d'échantillon, en valeur linéaire.

Un seul décodage par album : chaque piste est mesurée, puis toutes sont enchaînées pour le
gain d'album (seuils de la norme appliqués à l'album entier, comme rsgain ou loudgain).
FFmpeg affiche les mesures au dixième de dB : précision largement suffisante pour le volume.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .cache import check_cancel
from .models import Album

REFERENCE_LUFS = -18.0
OPUS_REFERENCE_LUFS = -23.0
SILENCE_LUFS = -70.0          # plancher d'ebur128 : piste muette, aucun gain proposé
RG_FIELDS = ("replaygain_track_gain", "replaygain_track_peak", "replaygain_album_gain", "replaygain_album_peak")
R128_FIELDS = ("r128_track_gain", "r128_album_gain")
SUMMARY = re.compile(r"\[ebur128@(\w+) @ [^\]]+\] Summary:(.*?)(?=\[ebur128@|\Z)", re.DOTALL)


class ReplayGainUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Loudness:
    integrated: float   # LUFS
    peak_db: float      # dBFS, crête d'échantillon

    @property
    def silent(self) -> bool:
        return self.integrated <= SILENCE_LUFS

    @property
    def peak(self) -> float:
        return 10 ** (self.peak_db / 20)


def has_replaygain(album: Album) -> bool:
    """D'après l'inventaire ; une piste lue avant la v0.22.0 n'a pas encore ces champs en cache."""
    return bool(album.tracks) and all(track.value("replaygain_track_gain") or track.value("r128_track_gain")
                                      for track in album.tracks)


def missing_replaygain(album: Album) -> bool:
    """Vrai si l'album n'a pas de ReplayGain : inventaire, sinon lecture de sa première piste."""
    if not album.tracks or has_replaygain(album):
        return False
    from .tagwriter import _Tags
    try:
        tags = _Tags(Path(album.ordered_tracks[0].path))
        return not (tags.get("replaygain_track_gain") or tags.get("r128_track_gain"))
    except Exception:  # noqa: BLE001 — fichier illisible : la mesure le signalera
        return True


def _parse(output: str) -> dict[str, Loudness]:
    found = {}
    for name, body in SUMMARY.findall(output):
        integrated = re.search(r"I:\s+(-?[\d.]+|-inf) LUFS", body)
        peak = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", body)
        if integrated and peak:
            value = lambda text: -200.0 if text == "-inf" else float(text)
            found[name] = Loudness(value(integrated.group(1)), value(peak.group(1)))
    return found


def measure_album(paths: list[str], ffmpeg: str, cancel=None, timeout: float = 3600) -> tuple[list[Loudness], Loudness]:
    """Mesure de chaque piste (dans l'ordre de `paths`) et de l'album enchaîné."""
    if not paths:
        raise ValueError("Album sans piste.")
    count = len(paths)
    chains = [f"[{index}:a:0]aresample=48000,asplit=2[t{index}][c{index}]" for index in range(count)]
    chains += [f"[t{index}]ebur128@piste{index}=peak=sample:framelog=verbose,anullsink" for index in range(count)]
    chains.append("".join(f"[c{index}]" for index in range(count))
                  + f"concat=n={count}:v=0:a=1,ebur128@album=peak=sample:framelog=verbose[sortie]")
    command = [ffmpeg, "-hide_banner", "-nostats", "-nostdin", "-loglevel", "info"]
    for path in paths:
        command += ["-i", os.path.abspath(path)]
    command += ["-filter_complex", ";".join(chains), "-map", "[sortie]", "-f", "null", "-"]
    options = {"stdout": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as log:
        # Journal dans un fichier : un tube plein bloquerait FFmpeg sur un long album.
        with subprocess.Popen(command, stderr=log, **options) as process:
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                try:
                    check_cancel(cancel)
                except BaseException:
                    process.kill()
                    raise
                if time.monotonic() > deadline:
                    process.kill()
                    raise ReplayGainUnavailable("FFmpeg ne répond pas (délai dépassé).")
                time.sleep(0.05)
        log.seek(0)
        output = log.read()
    found = _parse(output)
    tracks = [found.get(f"piste{index}") for index in range(count)]
    if process.returncode != 0 or None in tracks or "album" not in found:
        tail = " ".join(output.strip().splitlines()[-2:])[:300]
        raise ReplayGainUnavailable(f"Mesure FFmpeg impossible (fichier non décodable ?) : {tail}")
    return tracks, found["album"]


def _gain(loudness: Loudness, reference: float) -> float:
    return reference - loudness.integrated


def tag_values(track_loudness: Loudness, album_loudness: Loudness, codec: str) -> dict[str, str]:
    """Tags à écrire pour une piste ; vide si la piste est muette."""
    if track_loudness.silent:
        return {}
    if codec == "Opus":
        def q78(gain):
            return str(max(-32768, min(32767, round(gain * 256))))
        values = {"r128_track_gain": q78(_gain(track_loudness, OPUS_REFERENCE_LUFS))}
        if not album_loudness.silent:
            values["r128_album_gain"] = q78(_gain(album_loudness, OPUS_REFERENCE_LUFS))
        return values
    values = {"replaygain_track_gain": f"{_gain(track_loudness, REFERENCE_LUFS):.2f} dB",
              "replaygain_track_peak": f"{track_loudness.peak:.6f}"}
    if not album_loudness.silent:
        values.update(replaygain_album_gain=f"{_gain(album_loudness, REFERENCE_LUFS):.2f} dB",
                      replaygain_album_peak=f"{album_loudness.peak:.6f}")
    return values


def plan_album(album: Album, ffmpeg: str, cancel=None) -> dict[str, dict[str, str]]:
    """Valeurs par piste (chemin → tags), prêtes pour `plan_album_edits`."""
    tracks = album.ordered_tracks
    measured, whole = measure_album([track.path for track in tracks], ffmpeg, cancel)
    return {track.path: values for track, loudness in zip(tracks, measured)
            if (values := tag_values(loudness, whole, track.codec))}
