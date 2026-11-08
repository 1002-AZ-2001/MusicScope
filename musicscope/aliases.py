"""Alias d'artistes sourcés sans fusionner automatiquement les homonymes."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from .musicbrainz import MusicBrainzClient, ServiceUnavailable, UUID
from .normalization import name_key


@dataclass(frozen=True)
class ArtistCandidate:
    id: str
    name: str
    aliases: list[str]
    disambiguation: str
    url: str


@dataclass
class AliasResult:
    status: str
    candidates: list[ArtistCandidate] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


class ArtistAliasResolver:
    def __init__(self, cache_path: str | Path, opener=urlopen, **kwargs):
        self.client = MusicBrainzClient(cache_path, opener=opener, **kwargs)

    def close(self):
        self.client.close()

    def resolve(self, name: str) -> AliasResult:
        name = name.strip()
        if not name or len(name) > 500:
            raise ValueError("Un nom d'artiste de 1 à 500 caractères est nécessaire.")
        candidates = []
        try:
            query = f'artist:"{self.client._escape(name)}" OR alias:"{self.client._escape(name)}"'
            response = self.client._get("https://musicbrainz.org/ws/2/artist/?" + urlencode({
                "query": query, "fmt": "json", "limit": 10}))
            if not isinstance(response, dict) or not isinstance(response.get("artists"), list):
                raise ServiceUnavailable("Réponse de recherche d'artistes illisible.")
            results = response["artists"]
            seen = set()
            for summary in results[:10]:
                if not isinstance(summary, dict) or not isinstance(summary.get("id"), str) or not UUID.fullmatch(summary["id"]):
                    raise ServiceUnavailable("Identifiant d'artiste invalide dans la réponse.")
                mbid = summary["id"].lower()
                if mbid in seen:
                    continue
                seen.add(mbid)
                entity = self.client._get(f"https://musicbrainz.org/ws/2/artist/{mbid}?inc=aliases&fmt=json")
                if not isinstance(entity, dict) or entity.get("id") != mbid:
                    raise ServiceUnavailable("Réponse d'artiste illisible.")
                canonical = entity.get("name")
                aliases = entity.get("aliases", [])
                disambiguation = entity.get("disambiguation", "")
                if (not isinstance(canonical, str) or not isinstance(aliases, list)
                        or not isinstance(disambiguation, str)
                        or any(not isinstance(alias, dict) or not isinstance(alias.get("name"), str) for alias in aliases)):
                    raise ServiceUnavailable("Alias d'artiste invalides.")
                names = list(dict.fromkeys(alias["name"] for alias in aliases))
                candidates.append(ArtistCandidate(mbid, canonical, names, disambiguation,
                                                   "https://musicbrainz.org/artist/" + mbid))
            # Une graphie peut correspondre exactement à plusieurs artistes.
            # Même un résultat unique reste une proposition à valider.
            exact = [candidate for candidate in candidates if name_key(name) in {
                name_key(value) for value in [candidate.name, *candidate.aliases]}]
            total = response.get("count", response.get("artist-count", len(results) + (len(results) >= 10)))
            try:
                truncated = int(total) > len(results)
            except (TypeError, ValueError):
                truncated = True
            if len(exact) == 1 and not truncated:
                return AliasResult("Alias probable à valider", candidates,
                                   ["Une graphie canonique ou un alias sourcé concorde ; sélection explicite nécessaire.",
                                    "Les tags et dossiers restent inchangés."])
            if not candidates and not truncated:
                return AliasResult("Aucun résultat", reasons=["Aucun artiste trouvé pour cette graphie."])
            return AliasResult("Artiste indéterminé", candidates,
                               ["Homonymes, recherche tronquée ou graphie non confirmée : aucune fusion automatique."])
        except ServiceUnavailable as exc:
            return AliasResult("Service indisponible", candidates, [str(exc)])


Resolver = ArtistAliasResolver
