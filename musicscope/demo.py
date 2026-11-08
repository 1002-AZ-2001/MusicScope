"""Petit corpus sonore synthétique, sans téléchargement ni FFmpeg requis."""
import os
from importlib.resources import files
from pathlib import Path
from shutil import copyfile

from mutagen.flac import FLAC
from mutagen.id3 import ID3, TALB, TPE1, TPE2, TIT2, TRCK, TPOS, TDRC
from mutagen.mp3 import MP3


def create_demo(destination: Path) -> Path:
    destination = destination.expanduser().absolute()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Le dossier de démonstration doit être vide ou inexistant.")
    destination.mkdir(parents=True, exist_ok=True)

    def track(folder, num, tone="a", codec="flac", album="Horizons", disc=1, title=None,
              tracktotal=2, disctotal=1):
        directory = destination / "Artiste Démo" / folder
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{num:02d} - {title or ('Aube' if num == 1 else 'Voyage')}.{codec}"
        copyfile(str(files("musicscope").joinpath(f"data/tone-{tone}.{codec}")), path)
        tags = {"album": album, "albumartist": "Artiste Démo", "artist": "Artiste Démo",
                "title": title or ("Aube" if num == 1 else "Voyage"),
                "tracknumber": f"{num}/{tracktotal}", "discnumber": f"{disc}/{disctotal}", "date": "2024",
                "originaldate": "2020"}
        if codec == "flac":
            audio = FLAC(path)
            for key, value in tags.items():
                audio[key] = value
            audio.save()
        else:
            audio = MP3(path, ID3=ID3)
            if audio.tags is None:
                audio.add_tags()
            for frame, value in [(TALB, tags["album"]), (TPE1, tags["artist"]),
                                 (TPE2, tags["albumartist"]), (TIT2, tags["title"]),
                                 (TRCK, tags["tracknumber"]), (TPOS, tags["discnumber"]),
                                 (TDRC, tags["date"])]:
                audio.tags.add(frame(encoding=3, text=value))
            audio.save()
        return path

    originals = [track("Horizons FLAC", 1), track("Horizons FLAC", 2, "b")]
    copy_dir = destination / "Artiste Démo" / "Copie noms différents"
    copy_dir.mkdir()
    for index, source in enumerate(originals, 1):
        copyfile(source, copy_dir / f"copie-{index}.flac")
    for num, tone in [(1, "a"), (2, "b")]:
        track("Horizons MP3", num, tone, "mp3")
        track("Horizons Deluxe", num, tone, album="Horizons (Deluxe)", tracktotal=3)
    track("Horizons Deluxe", 3, "a", album="Horizons (Deluxe)", title="Bonus", tracktotal=3)
    track("Horizons incomplet", 1)
    track("Concert/CD1", 1, album="Concert", disc=1, tracktotal=1, disctotal=2)
    track("Concert/CD2", 1, "b", album="Concert", disc=2, tracktotal=1, disctotal=2)
    broken = track("Tags incohérents", 1, album="Essai")
    track("Tags incohérents", 1, "b", album="Autre titre", title="Numéro répété")
    audio = FLAC(broken)
    del audio["albumartist"]
    audio.save()
    link_dir = destination / "Artiste Démo" / "Liens physiques"
    try:
        link_dir.mkdir()
        for source in originals:
            os.link(source, link_dir / source.name)
    except OSError:
        # Certaines destinations réseau n'autorisent pas les liens physiques.
        for path in link_dir.iterdir():
            path.unlink()
        link_dir.rmdir()
    return destination
