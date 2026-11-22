"""Lecture des tags natifs uniquement ; l'écriture confirmée est dans tagwriter.py."""
from __future__ import annotations

import os
import re
from pathlib import Path

import mutagen

from .models import Track

AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".ogg", ".oga", ".opus"}


def _key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


ALIASES = {
    "title": ["title", "TIT2", "©nam"],
    "album": ["album", "TALB", "©alb"],
    "artist": ["artist", "TPE1", "©ART"],
    "albumartist": ["albumartist", "album artist", "TPE2", "aART"],
    "date": ["date", "year", "TDRC", "TYER", "©day"],
    "originaldate": ["originaldate", "originalyear", "TDOR", "TORY"],
    "releasecountry": ["releasecountry", "musicbrainzalbumreleasecountry"],
    "tracknumber": ["tracknumber", "TRCK"],
    "discnumber": ["discnumber", "TPOS"],
    "tracktotal": ["tracktotal", "totaltracks"],
    "disctotal": ["disctotal", "totaldiscs"],
    "musicbrainz_albumid": ["musicbrainzalbumid", "musicbrainzreleaseid"],
    "musicbrainz_releasegroupid": ["musicbrainzreleasegroupid"],
    "musicbrainz_artistid": ["musicbrainzartistid"],
    "musicbrainz_albumartistid": ["musicbrainzalbumartistid"],
    "musicbrainz_trackid": ["musicbrainztrackid", "musicbrainzrecordingid"],
    "compilation": ["compilation", "TCMP", "cpil"],
    "genre": ["genre", "TCON", "©gen"],
    "replaygain_track_gain": ["replaygain_track_gain"],
    "replaygain_album_gain": ["replaygain_album_gain"],
    "r128_track_gain": ["r128_track_gain"],
    "r128_album_gain": ["r128_album_gain"],
}

_ALIAS_KEYS = {canonical: [_key(alias) for alias in aliases] for canonical, aliases in ALIASES.items()}
# Pochettes (ID3 APIC, MP4 covr), données privées et objets encapsulés : sans alias.
_BINARY_PREFIXES = ("APIC", "PRIV", "GEOB", "MCDI", "SYLT", "RVA2", "POPM", "PCNT")
_BINARY_KEYS = {"covr", "metadata_block_picture"}


def _strings(value) -> list[str]:
    if hasattr(value, "text"):
        value = value.text
    if not isinstance(value, (list, tuple)):
        value = [value]
    result = []
    for item in value:
        if isinstance(item, bytes):
            item = item.decode("utf-8", errors="replace")
        text = str(item).strip("\0 \t\r\n")
        if text and text not in result:
            result.append(text)
    return result


def canonical_tags(native) -> dict[str, list[str]]:
    """Interprète ID3, Vorbis et les atomes MP4 sans dépendre de easy tags."""
    if not native:
        return {}
    values: dict[str, list[str]] = {}
    special = {"©nam": "title", "©alb": "album", "©ART": "artist", "©day": "date", "©gen": "genre"}
    for raw_key, raw_value in native.items():
        key = str(raw_key)
        if key.startswith(_BINARY_PREFIXES) or key in _BINARY_KEYS:
            continue  # pochettes et données binaires : jamais lues, coûteuses à convertir en texte
        if key.startswith("UFID:"):
            if key.removeprefix("UFID:").rstrip("/") == "http://musicbrainz.org":
                values["musicbrainztrackid"] = _strings(raw_value.data)
            continue
        if key in {"trkn", "disk"}:
            # MP4: une paire (numéro, total), pas une chaîne de texte.
            if not raw_value or len(raw_value[0]) != 2:
                continue  # atome vide ou malformé
            number, total = raw_value[0]
            prefix = "track" if key == "trkn" else "disc"
            if number:
                values[prefix + "number"] = [str(number)]
            if total:
                values[prefix + "total"] = [str(total)]
            continue
        if key.startswith("TXXX:"):
            key = key[5:]
        elif key.startswith("----:"):
            key = key.split(":", 2)[-1]
        key = _key(special.get(key, key))
        values.setdefault(key, []).extend(_strings(raw_value))
    tags = {}
    for canonical, keys in _ALIAS_KEYS.items():
        for alias in keys:
            if values.get(alias):
                tags[canonical] = list(dict.fromkeys(values[alias]))
                break
    for prefix in ("track", "disc"):
        number = tags.get(prefix + "number", [""])[0]
        if "/" in number and prefix + "total" not in tags:
            total = number.split("/", 1)[1].strip()
            if total.isdigit() and int(total):
                tags[prefix + "total"] = [total]
    return tags


def read_track(path: Path, stat: os.stat_result | None = None) -> Track:
    """Une erreur inattendue de décodage (fichier tronqué, atome malformé) est convertie en
    ValueError : elle marque ce fichier illisible sans interrompre tout le scan."""
    try:
        return _read_track(path, stat)
    except (OSError, ValueError):
        raise
    except Exception as exc:  # mutagen lève parfois IndexError, KeyError, struct.error…
        raise ValueError(f"Fichier audio corrompu ({type(exc).__name__}) : {exc}") from exc


def _read_track(path: Path, stat: os.stat_result | None = None) -> Track:
    path = Path(path)
    stat = stat or path.stat(follow_symlinks=False)
    try:
        audio = mutagen.File(path, easy=False)
    except mutagen.MutagenError as exc:
        raise ValueError(f"Lecture audio impossible : {exc}") from exc
    if audio is None or not getattr(audio, "info", None):
        raise ValueError("Format audio invalide ou non reconnu")
    kind = type(audio).__name__
    codec = {
        "FLAC": "FLAC", "MP3": "MP3", "OggVorbis": "Ogg Vorbis",
        "OggOpus": "Opus", "MP4": "AAC",
    }.get(kind, kind)
    if kind == "MP4":
        codec = "ALAC" if getattr(audio.info, "codec", "") == "alac" else "AAC"
    duration = getattr(audio.info, "length", None)
    return Track(path=str(path), codec=codec, size=stat.st_size,
                 mtime_ns=stat.st_mtime_ns, duration=duration,
                 tags=canonical_tags(audio.tags))
