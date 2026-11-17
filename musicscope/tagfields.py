"""Champs étendus de l'éditeur de tags et pochettes intégrées, pour chaque format.

Correspondances : celles de mutagen pour ID3 (EasyID3 : TCMP, TCOM, TPE3, UFID
http://musicbrainz.org pour ``musicbrainz_trackid``, TXXX:MusicBrainz …) et pour MP4
(atomes ©wrt, ©lyr, ©cmt, cpil ; atomes libres ``----:com.apple.iTunes:MusicBrainz …``),
noms Vorbis de MusicBrainz Picard pour FLAC, Ogg et Opus (MUSICBRAINZ_TRACKID désigne
l'enregistrement, MUSICBRAINZ_RELEASETRACKID la piste de l'édition).
"""
from __future__ import annotations

import base64
import hashlib

from mutagen.easyid3 import EasyID3
from mutagen.easymp4 import EasyMP4Tags
from mutagen.flac import Picture
from mutagen.id3 import APIC, COMM, USLT

# Champs absents d'un format, plutôt que d'inventer un tag que les autres logiciels ignoreraient.
UNSUPPORTED = {"MP4": {"conductor", "performer"}}
MAX_IMAGE_BYTES = 16 * 1024 * 1024
IMAGE_TYPES = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG\r\n\x1a\n": "image/png"}


def image_mime(data: bytes) -> str:
    for magic, mime in IMAGE_TYPES.items():
        if data.startswith(magic):
            return mime
    raise ValueError("Image non reconnue : choisissez un fichier JPEG ou PNG.")


def image_reference(data: bytes) -> str:
    """Valeur journalisée d'une pochette : « sha256:type ». Le contenu va dans le journal."""
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("Image vide ou trop volumineuse (16 Mio au plus).")
    return f"{hashlib.sha256(data).hexdigest()}:{image_mime(data)}"


# ID3 : paroles (USLT) et commentaire (COMM) sans description, langue quelconque.
def _id3_text_frames(frame_class, key):
    def getter(id3, _key):
        values = []
        for frame in id3.getall(key):
            if not frame.desc:  # USLT.text est une chaîne, COMM.text une liste
                values.extend([frame.text] if isinstance(frame.text, str) else [str(text) for text in frame.text])
        return values

    def setter(id3, _key, values):
        for frame in list(id3.getall(key)):
            if not frame.desc:
                id3.delall(frame.HashKey)
        if values:
            id3.add(frame_class(encoding=3, lang="und", desc="", text="\n".join(values)))

    def deleter(id3, _key):
        setter(id3, _key, [])
    return getter, setter, deleter


for _name, _frame, _key in (("lyrics", USLT, "USLT"), ("comment", COMM, "COMM")):
    if _name not in EasyID3.valid_keys:
        EasyID3.RegisterKey(_name, *_id3_text_frames(_frame, _key))

# MP4 : atomes standard iTunes et atomes libres MusicBrainz absents d'EasyMP4.
for _key, _atom in (("composer", "\xa9wrt"), ("lyrics", "\xa9lyr")):
    if _key not in EasyMP4Tags.Get:
        EasyMP4Tags.RegisterTextKey(_key, _atom)
for _key, _name in (("musicbrainz_releasegroupid", "MusicBrainz Release Group Id"),
                    ("musicbrainz_releasetrackid", "MusicBrainz Release Track Id")):
    if _key not in EasyMP4Tags.Get:
        EasyMP4Tags.RegisterFreeformKey(_key, _name)
if "compilation" not in EasyMP4Tags.Get:
    def _cpil_get(tags, key):
        return ["1"] if tags.get("cpil") else []

    def _cpil_set(tags, key, values):
        tags["cpil"] = bool(values and values[0] not in ("0", ""))

    def _cpil_delete(tags, key):
        tags.pop("cpil", None)
    EasyMP4Tags.RegisterKey("compilation", _cpil_get, _cpil_set, _cpil_delete)


# Pochette de face (type 3) intégrée.
def read_front_covers(audio, kind: str, id3=None, mp4=None) -> list[tuple[str, bytes]]:
    if kind == "FLAC":
        return [(picture.mime, picture.data) for picture in audio.pictures if picture.type in (0, 3)]
    if kind in ("OggVorbis", "OggOpus", "OggFLAC"):
        covers = []
        for value in audio.tags.get("metadata_block_picture", []) if audio.tags else []:
            try:
                picture = Picture(base64.b64decode(value))
            except (ValueError, TypeError):
                continue
            if picture.type in (0, 3):
                covers.append((picture.mime, picture.data))
        return covers
    if kind == "MP3" and id3 is not None:
        return [(frame.mime, frame.data) for frame in id3.getall("APIC") if frame.type in (0, 3)]
    if kind == "MP4" and mp4 is not None:
        return [("image/png" if cover.imageformat == cover.FORMAT_PNG else "image/jpeg", bytes(cover))
                for cover in mp4.get("covr", [])]
    return []


def write_front_cover(audio, kind: str, image: tuple[str, bytes] | None, id3=None, mp4=None) -> None:
    """Remplace la pochette de face ; `image` None la retire. Les autres images sont gardées."""
    if kind == "FLAC":
        kept = [picture for picture in audio.pictures if picture.type not in (0, 3)]
        audio.clear_pictures()
        for picture in kept:
            audio.add_picture(picture)
        if image is not None:
            audio.add_picture(_picture(*image))
        return
    if kind in ("OggVorbis", "OggOpus", "OggFLAC"):
        kept = []
        for value in audio.tags.get("metadata_block_picture", []):
            try:
                if Picture(base64.b64decode(value)).type not in (0, 3):
                    kept.append(value)
            except (ValueError, TypeError):
                kept.append(value)
        if image is not None:
            kept.append(base64.b64encode(_picture(*image).write()).decode("ascii"))
        if kept:
            audio.tags["metadata_block_picture"] = kept
        elif "metadata_block_picture" in audio.tags:
            del audio.tags["metadata_block_picture"]
        return
    if kind == "MP3" and id3 is not None:
        for frame in list(id3.getall("APIC")):
            if frame.type in (0, 3):
                id3.delall(frame.HashKey)
        if image is not None:
            id3.add(APIC(encoding=3, mime=image[0], type=3, desc="", data=image[1]))
        return
    if kind == "MP4" and mp4 is not None:
        from mutagen.mp4 import MP4Cover
        if image is None:
            mp4.pop("covr", None)
        else:
            form = MP4Cover.FORMAT_PNG if image[0] == "image/png" else MP4Cover.FORMAT_JPEG
            mp4["covr"] = [MP4Cover(image[1], imageformat=form)]
        return
    raise ValueError("Pochette intégrée non prise en charge pour ce format.")


def _picture(mime: str, data: bytes) -> Picture:
    picture = Picture()
    picture.type, picture.mime, picture.desc, picture.data = 3, mime, "", data
    return picture
