"""Identifier plusieurs albums sur MusicBrainz et n'en écrire que les identifiants.

Chaque album passe par la même vérification que l'album seul (`verify_album`) : une seule
édition cohérente avec les pistes locales, sinon rien n'est proposé. Seuls les identifiants
MusicBrainz sont repris ; titres, dates et noms restent ceux de l'utilisateur. MusicBrainz
demande au plus une requête par seconde : le client partagé la respecte, et son cache évite
de redemander une réponse récente.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .cache import check_cancel
from .musicbrainz import UUID, Verification
from .tagwriter import MBID_FIELDS, FileEdit, plan_album_edits, proposals_to_values

PROBABLE = "Édition probable"
SECONDS_PER_REQUEST = 1.0


@dataclass
class AlbumResult:
    album: object
    verification: Verification
    album_values: dict = field(default_factory=dict)
    track_values: dict = field(default_factory=dict)

    @property
    def probable(self) -> bool:
        return self.verification.status == PROBABLE

    @property
    def writable(self) -> bool:
        return self.probable and bool(self.album_values or self.track_values)

    @property
    def url(self) -> str:
        candidates = self.verification.candidates
        return candidates[0].url if self.probable and candidates else ""

    @property
    def count(self) -> int:
        return len(self.album_values) * len(self.album.tracks) + sum(len(v) for v in self.track_values.values())


def estimate(albums) -> tuple[float, float]:
    """Durée habituelle et durée au pire (secondes), sans compter le cache. Un MBID d'édition
    valide coûte une requête (sept s'il s'avère incohérent) ; sinon une recherche et jusqu'à
    cinq éditions (six requêtes)."""
    usual = worst = 0
    for album in albums:
        ids = album.values("musicbrainz_albumid")
        if len(ids) == 1 and UUID.fullmatch(next(iter(ids))):
            usual, worst = usual + 1, worst + 7
        else:
            usual, worst = usual + 6, worst + 6
    return usual * SECONDS_PER_REQUEST, worst * SECONDS_PER_REQUEST


def identifiers(verification: Verification, album) -> tuple[dict, dict]:
    """Valeurs d'identifiants seulement, d'après les propositions d'une édition probable."""
    if verification.status != PROBABLE:
        return {}, {}
    proposals = [proposal for proposal in verification.proposals if proposal.field in MBID_FIELDS]
    return proposals_to_values(proposals, album)


def verify_albums(albums, client, cancel=None, progress=None) -> list[AlbumResult]:
    results = []
    for position, album in enumerate(albums, 1):
        check_cancel(cancel)
        if progress:
            progress(position, len(albums), f"MusicBrainz : {album.path}")
        verification = client.verify_album(album)
        results.append(AlbumResult(album, verification, *identifiers(verification, album)))
    return results


def identifier_edits(results, chosen=None, live_values=None) -> list[FileEdit]:
    """Fichiers à modifier pour les albums `chosen` (chemins ; tous les inscriptibles par défaut)."""
    edits = []
    for result in results:
        if not result.writable or (chosen is not None and result.album.path not in chosen):
            continue
        live = live_values(result.album) if live_values else {}
        edits.extend(plan_album_edits(result.album, result.album_values, result.track_values, live))
    return edits
