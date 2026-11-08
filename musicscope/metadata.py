"""Lecture des tags natifs uniquement : aucune écriture de métadonnées."""
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
}


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
    special = {"©nam": "title", "©alb": "album", "©ART": "artist", "©day": "date"}
    for raw_key, raw_value in native.items():
        key = str(raw_key)
        if key.startswith("UFID:"):
            if key.removeprefix("UFID:").rstrip("/") == "http://musicbrainz.org":
                values["musicbrainztrackid"] = _strings(raw_value.data)
            continue
        if key in {"trkn", "disk"}:
            # MP4: une paire (numéro, total), pas une chaîne de texte.
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
    for canonical, aliases in ALIASES.items():
        for alias in aliases:
            if values.get(_key(alias)):
                tags[canonical] = list(dict.fromkeys(values[_key(alias)]))
                break
    for prefix in ("track", "disc"):
        number = tags.get(prefix + "number", [""])[0]
        if "/" in number and prefix + "total" not in tags:
            total = number.split("/", 1)[1].strip()
            if total.isdigit() and int(total):
                tags[prefix + "total"] = [total]
    return tags


def read_track(path: Path, stat: os.stat_result | None = None) -> Track:
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
