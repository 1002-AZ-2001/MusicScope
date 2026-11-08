"""Constats locaux, sans supposer que les artistes invités sont des erreurs."""
from collections import Counter, defaultdict

from .models import Album
from .normalization import name_key


def audit_album(album: Album) -> list[str]:
    issues = []
    if not album.complete:
        issues.append("Inventaire incomplet : au moins un fichier n'a pas pu être lu.")
    if not album.tracks:
        return issues + ["Aucune piste lisible dans ce dossier."]
    for key, label in (("album", "Album"), ("albumartist", "Artiste d'album"),
                       ("artist", "Artiste"), ("title", "Titre"), ("date", "Date d'édition")):
        missing = sum(not t.value(key) for t in album.tracks)
        if missing:
            issues.append(f"{label} manquant : {missing} piste(s).")
    # Les artistes de piste et dates originales peuvent varier dans une compilation.
    for key, label in (("album", "Titres d'album"), ("albumartist", "Artistes d'album"),
                       ("date", "Dates d'édition"), ("musicbrainz_albumid", "MBID d'édition"),
                       ("musicbrainz_releasegroupid", "MBID de groupe d'éditions")):
        values = album.values(key)
        normalized = ({tuple(sorted(name_key(value) for value in signature))
                       for signature in album.tag_signatures(key)} if key == "albumartist"
                      else {name_key(value) for value in values})
        if len(normalized) > 1:
            issues.append(f"{label} incohérents : {' ; '.join(sorted(values))}.")
    discs = defaultdict(list)
    for track in album.tracks:
        discs[track.disc].append(track)
    disc_numbers = sorted(discs)
    if max(disc_numbers) > 1000 or min(disc_numbers) < 1:
        issues.append("Numérotation de disque anormalement élevée ou invalide.")
    elif disc_numbers != list(range(1, max(disc_numbers) + 1)):
        issues.append(f"Disques potentiellement manquants : numéros présents {disc_numbers}.")
    disc_totals = {t.number("disctotal") for t in album.tracks if t.number("disctotal")}
    if len(disc_totals) > 1:
        issues.append("Nombre total de disques incohérent entre les pistes.")
    elif disc_totals and max(disc_totals) != len(discs):
        issues.append(f"Nombre de disques à vérifier : {len(discs)} présent(s), {max(disc_totals)} annoncé(s).")
    for disc, tracks in sorted(discs.items()):
        numbers = [t.number_in_disc for t in tracks]
        if any(n <= 0 for n in numbers):
            issues.append(f"Disque {disc} : numéro de piste absent ou invalide.")
        repeats = sorted(n for n, count in Counter(numbers).items() if n > 0 and count > 1)
        if repeats:
            issues.append(f"Disque {disc} : numéros de piste répétés {repeats}.")
        totals = {t.number("tracktotal") for t in tracks if t.number("tracktotal")}
        if len(totals) > 1:
            issues.append(f"Disque {disc} : nombre total de pistes incohérent.")
        limit = max(totals | set(numbers))
        # Ne pas allouer une énorme plage pour un tag erroné.
        if limit > 10000:
            issues.append(f"Disque {disc} : numérotation de piste anormalement élevée ({limit}).")
        elif limit:
            missing = sorted(set(range(1, limit + 1)) - set(numbers))
            if missing:
                issues.append(f"Disque {disc} : pistes potentiellement manquantes {missing}.")
        if totals and any(n > max(totals) for n in numbers):
            issues.append(f"Disque {disc} : numéro de piste supérieur au total annoncé.")
    return issues
