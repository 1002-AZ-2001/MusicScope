"""Vérification distante facultative. Aucune écriture dans les fichiers audio.

Les propositions restent des observations : même une édition probable n'est
jamais appliquée aux tags. Une liste de pistes ne suffit pas à départager deux
éditions MusicBrainz qui possèdent les mêmes enregistrements.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import Album, Track
from . import __version__
from .comparison import _complete
from .normalization import name_key

API = "https://musicbrainz.org/ws/2/release/"
USER_AGENT = f"MusicScope/{__version__} (https://github.com/1002-AZ-2001/MusicScope)"
UUID = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")


@dataclass
class TagProposal:
    path: str
    field: str
    current: str
    proposed: str
    reason: str
    url: str


@dataclass
class ReleaseCandidate:
    id: str
    title: str = ""
    artist: str = ""
    date: str = ""
    country: str = ""
    disambiguation: str = ""
    score: float = 0
    reasons: list[str] = field(default_factory=list)
    url: str = ""
    plausible: bool = False


@dataclass
class Verification:
    status: str
    candidates: list[ReleaseCandidate] = field(default_factory=list)
    proposals: list[TagProposal] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


class ServiceUnavailable(Exception):
    """Échec HTTP, transport ou réponse illisible, distinct d'un résultat vide."""


class _RateLimiter:
    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.last = float("-inf")

    def request(self, opener, request):
        # Une seule requête en vol, quel que soit le nombre de clients GUI.
        with self.lock:
            delay = 1.0 - (self.clock() - self.last)
            if delay > 0:
                self.sleep(delay)
            self.last = self.clock()
            with opener(request, timeout=20) as response:
                body = response.read(8 * 1024 * 1024 + 1)
            if len(body) > 8 * 1024 * 1024:
                raise ServiceUnavailable("Réponse MusicBrainz trop volumineuse.")
            return body


_SHARED_LIMITER = _RateLimiter()


def _credit(credit: list | None) -> str:
    return "".join(
        entry if isinstance(entry, str) else
        entry.get("name", entry.get("artist", {}).get("name", "")) + entry.get("joinphrase", "")
        for entry in credit or []
    )


def _artist_ids(credit: list | None) -> str:
    return "; ".join(dict.fromkeys(
        entry["artist"]["id"] for entry in credit or []
        if isinstance(entry, dict) and entry.get("artist", {}).get("id")
    ))


def _int(value, default=0):
    try:
        return int(value)
    except (ValueError, TypeError, OverflowError):
        return default


def _remote_tracks(release: dict) -> list[tuple[int, int, dict]]:
    result = []
    for medium in release.get("media", []):
        disc = _int(medium.get("position"))
        for track in medium.get("tracks", []):
            result.append((disc, _int(track.get("position")), track))
    return sorted(result, key=lambda item: (item[0], item[1]))


def _title(track: dict) -> str:
    return track.get("title") or track.get("recording", {}).get("title", "")


def _duration(track: dict) -> float | None:
    length = track.get("length") or track.get("recording", {}).get("length")
    try:
        duration = float(length) / 1000 if length is not None else None
        return duration if duration is not None and math.isfinite(duration) and duration > 0 else None
    except (ValueError, TypeError):
        return None


def _date_parts(value: str) -> tuple[int, ...] | None:
    if not re.fullmatch(r"\d{4}(?:-\d{2}){0,2}", value):
        return None
    parts = tuple(int(part) for part in value.split("-"))
    if not 1 <= parts[0] <= 9999 or (len(parts) > 1 and not 1 <= parts[1] <= 12):
        return None
    if len(parts) > 2:
        try:
            datetime(*parts)
        except ValueError:
            return None
    return parts


def _same_date(left: str, right: str) -> bool:
    a, b = _date_parts(left), _date_parts(right)
    return bool(a and b and a[:min(len(a), len(b))] == b[:min(len(a), len(b))])


def _signatures(album: Album, key: str) -> set[tuple[str, ...]]:
    return {tuple(sorted(name_key(value) for value in signature))
            for signature in album.tag_signatures(key)}


def _same_artists(album: Album, credit: list) -> bool:
    key = "albumartist" if album.values("albumartist") else "artist"
    signatures = _signatures(album, key)
    if len(signatures) != 1:
        return False
    remote = tuple(sorted(name_key(entry.get("name", entry.get("artist", {}).get("name", "")))
                          for entry in credit if isinstance(entry, dict)))
    return (next(iter(signatures)) == remote
            or name_key(album.value(key)) == name_key(_credit(credit)))


def _assess(album: Album, release: dict) -> ReleaseCandidate:
    candidate = ReleaseCandidate(
        id=release["id"], title=release.get("title", ""),
        artist=_credit(release.get("artist-credit")), date=release.get("date", ""),
        country=release.get("country", ""), disambiguation=release.get("disambiguation", ""),
        url="https://musicbrainz.org/release/" + release["id"],
    )
    reasons, conflicts = candidate.reasons, []
    local_title = album.value("album")
    local_artist = album.value("albumartist") or album.value("artist")
    if local_title and name_key(local_title) == name_key(candidate.title):
        candidate.score += 20
    elif local_title:
        conflicts.append(f"Titre d'album différent : {local_title} / {candidate.title}.")
    else:
        reasons.append("Titre d'album local absent.")
    if local_artist and _same_artists(album, release.get("artist-credit", [])):
        candidate.score += 15
    elif local_artist:
        # Un alias peut expliquer cet écart, mais nous ne l'affirmons pas.
        conflicts.append(f"Artiste d'album différent ou alias à vérifier : {local_artist} / {candidate.artist}.")
    else:
        reasons.append("Artiste d'album local absent.")
    if len(_signatures(album, "album")) > 1:
        conflicts.append("Titres d'album incohérents entre les pistes locales.")
    dates = album.values("date")
    if len(dates) > 1:
        conflicts.append("Dates d'édition incohérentes entre les pistes locales.")
    elif dates:
        local_date = next(iter(dates))
        if not _date_parts(local_date):
            conflicts.append(f"Date d'édition locale non interprétable : {local_date}.")
        elif candidate.date and not _same_date(local_date, candidate.date):
            conflicts.append(f"Date d'édition différente : {local_date} / {candidate.date}.")
        elif candidate.date:
            reasons.append(f"Date d'édition concordante à la précision disponible : {local_date} / {candidate.date}.")
        else:
            reasons.append("Date d'édition absente de MusicBrainz : indice local non départagé.")
    countries = {value.upper() for value in album.values("releasecountry")}
    if len(countries) > 1:
        conflicts.append("Pays d'édition incohérents entre les pistes locales.")
    elif countries and candidate.country:
        local_country = next(iter(countries))
        if local_country != candidate.country.upper():
            conflicts.append(f"Pays d'édition différent : {local_country} / {candidate.country}.")
        else:
            reasons.append(f"Pays d'édition concordant : {local_country}.")
    if album.values("originaldate"):
        original = release.get("release-group", {}).get("first-release-date", "")
        reasons.append("Les dates originales sont présentées comme propositions distinctes de la date d'édition"
                       + (f" ; première date du groupe MusicBrainz : {original}." if original else ".")
                       + " Elles peuvent varier entre les pistes d'une compilation.")
    remote = _remote_tracks(release)
    local = album.ordered_tracks
    if not _complete(album):
        conflicts.append("Inventaire local incomplet ou numérotation/totaux indiquant des pistes ou disques manquants.")
    if any("regroupement ambigu" in issue.casefold() for issue in album.issues):
        conflicts.append("Regroupement local ambigu : vérifier les dossiers de disques avant d'identifier une édition.")
    if not local or not remote:
        conflicts.append("Liste de pistes indisponible.")
    if len(local) != len(remote):
        conflicts.append(f"Nombre de pistes différent : {len(local)} locales / {len(remote)} MusicBrainz.")
    elif local:
        candidate.score += 10
    local_discs = {t.disc for t in local}
    remote_discs = {disc for disc, _, _ in remote}
    if local_discs == remote_discs and local_discs:
        candidate.score += 10
    else:
        conflicts.append(f"Disques différents : {sorted(local_discs)} / {sorted(remote_discs)}.")
    positions = [(t.disc, t.number_in_disc) for t in local]
    if len(positions) != len(set(positions)) or any(number <= 0 for _, number in positions):
        conflicts.append("Numéros locaux absents ou répétés.")
    matched = 0
    for track, (disc, number, reference) in zip(local, remote):
        label = f"Disque {disc}, piste {number}"
        if (track.disc, track.number_in_disc) != (disc, number):
            conflicts.append(f"{label} : ordre ou numéro différent.")
        recording_id = reference.get("recording", {}).get("id", "")
        local_ids = [value.lower() for value in track.tags.get("musicbrainz_trackid", [])]
        recording_matches = bool(recording_id and local_ids == [recording_id])
        if local_ids and recording_id and not recording_matches:
            conflicts.append(f"{label} : identifiant d'enregistrement différent.")
        title_matches = bool(track.value("title") and name_key(track.value("title")) == name_key(_title(reference)))
        if track.value("title") and not title_matches:
            conflicts.append(f"{label} : titre différent ({track.value('title')} / {_title(reference)}).")
        remote_duration = _duration(reference)
        durations_known = (track.duration is not None and math.isfinite(track.duration)
                           and track.duration > 0 and remote_duration is not None)
        duration_matches = durations_known and abs(track.duration - remote_duration) <= 2.0
        if durations_known and not duration_matches:
            conflicts.append(f"{label} : écart de durée {abs(track.duration - remote_duration):.1f} s.")
        if recording_matches or (title_matches and duration_matches):
            matched += 1
        else:
            conflicts.append(f"{label} : preuves insuffisantes pour rapprocher l'enregistrement.")
    if local:
        candidate.score += 45 * matched / len(local)
    if remote:
        reasons.append(f"{matched}/{len(local)} pistes rapprochées par titre et durée ou MBID d'enregistrement.")
    reasons.extend(dict.fromkeys(conflicts))
    candidate.plausible = bool(local and remote and not conflicts)
    if candidate.plausible:
        reasons.append("Disques, ordre et enregistrements cohérents ; l'édition reste probable.")
    return candidate


def _proposals(album: Album, release: dict, candidate: ReleaseCandidate) -> list[TagProposal]:
    result = []
    group = release.get("release-group", {})
    for local, (disc, number, reference) in zip(album.ordered_tracks, _remote_tracks(release)):
        recording = reference.get("recording", {})
        artist_credit = reference.get("artist-credit") or recording.get("artist-credit")
        values = {
            "album": release.get("title", ""), "albumartist": candidate.artist,
            "date": release.get("date", ""), "originaldate": group.get("first-release-date", ""),
            "releasecountry": release.get("country", ""),
            "musicbrainz_albumid": candidate.id, "musicbrainz_releasegroupid": group.get("id", ""),
            "musicbrainz_albumartistid": _artist_ids(release.get("artist-credit")),
            "title": _title(reference), "tracknumber": str(number), "discnumber": str(disc),
            "musicbrainz_trackid": recording.get("id", ""),
            "musicbrainz_artistid": _artist_ids(artist_credit), "artist": _credit(artist_credit),
        }
        for key, value in values.items():
            current = local.value(key)
            if key in {"tracknumber", "discnumber"} and local.number(key) == _int(value):
                continue
            if value and current != value:
                reason = ("Première date du groupe d'éditions MusicBrainz ; vérifier le sens des dates d'origine des pistes, notamment pour une compilation."
                          if key == "originaldate" else "Valeur de l'édition probable MusicBrainz, à valider manuellement.")
                result.append(TagProposal(local.path, key, current, value, reason, candidate.url))
    return result


class MusicBrainzClient:
    """Client sérialisé, limité à 1 requête/s, avec cache SQLite de 7 jours.

    ``opener`` et les horloges sont injectables pour tester sans réseau. Chaque
    tâche doit fermer son client. Les secrets et les fichiers audio ne sont pas
    utilisés par ce service public.
    """

    def __init__(self, cache_path: str | Path, opener=urlopen, clock=time.monotonic,
                 sleep=time.sleep, wall_clock=time.time, cache_ttl=7 * 86400):
        path = Path(cache_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS mb_responses (url TEXT PRIMARY KEY, fetched REAL, body TEXT)")
        self.db.commit()
        self.lock = threading.RLock()
        self.opener, self.sleep, self.wall_clock, self.cache_ttl = opener, sleep, wall_clock, cache_ttl
        self.limiter = _SHARED_LIMITER if clock is time.monotonic and sleep is time.sleep else _RateLimiter(clock, sleep)

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _get(self, url: str) -> dict | None:
        with self.lock:
            row = self.db.execute("SELECT fetched, body FROM mb_responses WHERE url=?", (url,)).fetchone()
            if row and 0 <= self.wall_clock() - row[0] < self.cache_ttl:
                try:
                    cached = json.loads(row[1])
                    if isinstance(cached, dict):
                        return cached
                except (ValueError, TypeError):
                    pass
            for attempt in range(3):
                try:
                    body = self.limiter.request(self.opener, Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}))
                    result = json.loads(body)
                    if not isinstance(result, dict):
                        raise ValueError("Objet JSON attendu")
                    self.db.execute("INSERT OR REPLACE INTO mb_responses VALUES (?, ?, ?)",
                                    (url, self.wall_clock(), json.dumps(result, ensure_ascii=False)))
                    self.db.commit()
                    return result
                except HTTPError as error:
                    if error.code == 404:
                        return None
                    if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                        raise ServiceUnavailable(f"MusicBrainz indisponible (HTTP {error.code}).") from error
                    retry = error.headers.get("Retry-After", "") if error.headers else ""
                    try:
                        delay = float(retry)
                    except (ValueError, TypeError):
                        try:
                            delay = (parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()
                        except (ValueError, TypeError, OverflowError):
                            delay = 2 ** attempt
                    # Un délai serveur long ne doit pas être contourné par une relance prématurée.
                    if delay > 10:
                        raise ServiceUnavailable("MusicBrainz demande de réessayer plus tard.") from error
                    self.sleep(max(1, delay))
                except (URLError, TimeoutError, OSError) as error:
                    if attempt == 2:
                        raise ServiceUnavailable("MusicBrainz inaccessible (réseau ou délai dépassé).") from error
                    self.sleep(2 ** attempt)
                except (ValueError, UnicodeError) as error:
                    raise ServiceUnavailable("Réponse MusicBrainz illisible.") from error
            raise ServiceUnavailable("MusicBrainz indisponible.")

    def _release(self, mbid: str) -> dict | None:
        release = self._get(API + mbid + "?" + urlencode({"inc": "recordings+artist-credits+release-groups", "fmt": "json"}))
        if release is not None:
            valid = release.get("id") == mbid and isinstance(release.get("media"), list)
            valid = valid and all(isinstance(release.get(key, ""), str)
                                  for key in ("title", "date", "country", "disambiguation"))
            valid = valid and isinstance(release.get("release-group", {}), dict)
            if valid:
                valid = all(isinstance(release.get("release-group", {}).get(key, ""), str)
                            for key in ("id", "first-release-date"))
            valid = valid and self._valid_credit(release.get("artist-credit", []))
            for medium in release.get("media", []) if valid else []:
                if not isinstance(medium, dict) or not isinstance(medium.get("tracks", []), list):
                    valid = False
                    break
                for track in medium.get("tracks", []):
                    if not isinstance(track, dict) or not isinstance(track.get("recording", {}), dict):
                        valid = False
                        break
                    recording = track.get("recording", {})
                    if (not isinstance(track.get("title", ""), str)
                            or not isinstance(recording.get("title", ""), str)
                            or not isinstance(recording.get("id", ""), str)
                            or not self._valid_credit(track.get("artist-credit", []))
                            or not self._valid_credit(recording.get("artist-credit", []))):
                        valid = False
                        break
            if not valid:
                raise ServiceUnavailable("Réponse d'édition MusicBrainz incomplète.")
        return release

    @staticmethod
    def _valid_credit(credit) -> bool:
        return isinstance(credit, list) and all(
            isinstance(entry, str) or (isinstance(entry, dict)
                and isinstance(entry.get("artist", {}), dict)
                and isinstance(entry.get("name", ""), str)
                and isinstance(entry.get("joinphrase", ""), str)
                and isinstance(entry.get("artist", {}).get("id", ""), str)
                and isinstance(entry.get("artist", {}).get("name", ""), str))
            for entry in credit)

    @staticmethod
    def _escape(value: str) -> str:
        return re.sub(r'([+\-!(){}\[\]^"~*?:\\/|&])', r"\\\1", value)

    def verify_album(self, album: Album) -> Verification:
        candidates: list[ReleaseCandidate] = []
        reasons: list[str] = []
        releases: dict[str, dict] = {}
        try:
            ids = album.values("musicbrainz_albumid")
            if len(ids) == 1 and UUID.fullmatch(next(iter(ids))):
                mbid = next(iter(ids)).lower()
                release = self._release(mbid)
                if release:
                    candidate = _assess(album, release)
                    candidates.append(candidate)
                    releases[mbid] = release
                    if candidate.plausible:
                        return Verification("Édition probable", candidates, _proposals(album, release, candidate),
                                            ["MBID d'édition local vérifié avec les pistes MusicBrainz."])
                    reasons.append("MBID local incohérent avec les pistes ou les tags ; recherche complémentaire.")
                else:
                    reasons.append("MBID local introuvable ; recherche complémentaire.")
            elif ids:
                reasons.append("MBID d'édition local invalide ou contradictoire ; recherche complémentaire.")
            artist = album.value("albumartist") or album.value("artist")
            title = album.value("album")
            if not artist or not title or len(album.values("album")) != 1:
                return Verification("Édition indéterminée", candidates, reasons=reasons + ["Artiste et titre d'album cohérents nécessaires à la recherche."])
            query = f'artist:"{self._escape(artist)}" AND release:"{self._escape(title)}"'
            response = self._get(API + "?" + urlencode({"query": query, "fmt": "json", "limit": 5}))
            if response is None or not isinstance(response.get("releases"), list):
                raise ServiceUnavailable("Réponse de recherche MusicBrainz incomplète.")
            results = response["releases"]
            for summary in results[:5]:
                if not isinstance(summary, dict) or not isinstance(summary.get("id", ""), str):
                    raise ServiceUnavailable("Résultat de recherche MusicBrainz illisible.")
                mbid = summary.get("id", "")
                if not UUID.fullmatch(mbid) or mbid in releases:
                    continue
                release = self._release(mbid)
                if release:
                    releases[mbid] = release
                    candidates.append(_assess(album, release))
            candidates.sort(key=lambda c: (not c.plausible, -c.score, c.id))
            plausible = [c for c in candidates if c.plausible]
            # Une recherche tronquée peut cacher une autre édition également plausible.
            total = response.get("count", response.get("release-count"))
            truncated = _int(total, len(results) + (len(results) >= 5)) > len(results)
            if len(plausible) == 1 and not truncated:
                winner = plausible[0]
                return Verification("Édition probable", candidates,
                                    _proposals(album, releases[winner.id], winner), reasons + ["Une édition cohérente parmi les résultats de la recherche."])
            if len(plausible) > 1:
                reasons.append("Plusieurs éditions ont des pistes cohérentes ; aucune édition choisie automatiquement.")
            elif truncated:
                reasons.append("Recherche limitée à cinq éditions ; d'autres éditions peuvent convenir.")
            elif candidates:
                reasons.append("Aucune édition examinée ne concorde suffisamment avec l'inventaire local.")
            return Verification("Édition indéterminée" if candidates or results or truncated else "Aucun résultat",
                                candidates, reasons=reasons or ["Aucun résultat pour cet artiste et ce titre."])
        except ServiceUnavailable as error:
            return Verification("Service indisponible", candidates, reasons=reasons + [str(error)])
