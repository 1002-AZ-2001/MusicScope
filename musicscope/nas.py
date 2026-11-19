"""Travail lourd fait sur le NAS, importé ensuite par l'interface.

Sur le NAS, un conteneur lit la médiathèque en local et en lecture seule : tags, SHA-256
des fichiers de même taille (candidats aux doublons exacts) et, si FFmpeg est présent,
spectre des FLAC. Il dépose un fichier « musicscope-nas.json.gz » dont les chemins sont
relatifs à la racine musicale. Sur le poste, l'import rattache chaque fichier à la même
racine vue par SMB : un fichier n'est repris que si sa taille est identique et sa date de
modification égale à la microseconde près (SMB transmet les dates par pas de 100 ns).
Aucun port réseau n'est ouvert, rien n'est écrit dans la médiathèque.
"""
from __future__ import annotations

import gzip
import json
import os
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from . import __version__
from .cache import Cache, check_cancel
from .models import Track

FORMAT = "musicscope-nas"
VERSION = 1
EXPORT_NAME = "musicscope-nas.json.gz"
MTIME_TOLERANCE_NS = 1_000          # SMB : pas de 100 ns
MAX_UNCOMPRESSED = 2 * 1024 ** 3


@dataclass
class WorkReport:
    tracks: int = 0
    hashed: int = 0
    spectra: int = 0
    errors: list[str] = field(default_factory=list)
    path: str = ""
    seconds: float = 0.0


@dataclass
class ImportReport:
    imported: int = 0
    hashes: int = 0
    spectra: int = 0
    missing: int = 0
    changed: int = 0
    root: str = ""
    created_at: str = ""


def _relative(path: str, root: Path) -> str:
    return PurePosixPath(Path(path).relative_to(root).as_posix()).as_posix()


def run_work(root: str | Path, out_dir: str | Path, cache_dir: str | Path, spectrum: str = "one",
             hashes: bool = True, cancel=None, log: Callable[[str], None] = print) -> WorkReport:
    """Scan, SHA-256 des candidats et spectre ; puis export atomique du résultat."""
    from .scanner import scan
    started = time.monotonic()
    root, out_dir, cache_dir = Path(root).absolute(), Path(out_dir), Path(cache_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir.mkdir(parents=True, exist_ok=True)
    report = WorkReport()
    with Cache(cache_dir / "inventory.sqlite3") as cache:
        log(f"Analyse de {root}…")
        inventory = scan(root, cache, cancel=cancel)
        tracks = [track for album in inventory.albums for track in album.tracks]
        report.tracks, report.errors = len(tracks), list(inventory.errors)
        log(f"{len(tracks)} pistes, {len(inventory.errors)} erreur(s) de lecture.")
        digests = {}
        if hashes:
            by_size = defaultdict(list)
            for track in tracks:
                by_size[track.size].append(track)
            candidates = [track for group in by_size.values() if len(group) > 1 for track in group]
            log(f"SHA-256 de {len(candidates)} fichiers de même taille…")
            for position, track in enumerate(candidates, 1):
                check_cancel(cancel)
                try:
                    digests[track.path] = cache.hash_track(track, cancel)
                except OSError as exc:
                    report.errors.append(f"SHA-256 impossible : {track.path} : {exc}")
                if position % 1000 == 0:
                    log(f"  {position}/{len(candidates)}")
            report.hashed = len(digests)
    results = {}
    if spectrum != "none":
        from .spectrum import SpectrumCache, SpectrumUnavailable, analyze
        try:
            with SpectrumCache(cache_dir / "spectrum.sqlite3") as spectra:
                log("Spectre des FLAC…")
                results = analyze(inventory.albums, spectra, 1 if spectrum == "one" else None, cancel)
        except SpectrumUnavailable as exc:
            report.errors.append(str(exc))
        report.spectra = len(results)
    payload = {
        "format": FORMAT, "version": VERSION, "app_version": __version__, "root": str(root),
        "created_at": datetime.now(UTC).isoformat(),
        "tracks": [{"rel": _relative(track.path, root), "size": track.size, "mtime_ns": track.mtime_ns,
                    "codec": track.codec, "duration": track.duration, "tags": track.tags} for track in tracks],
        "hashes": [{"rel": _relative(path, root), "size": track.size, "mtime_ns": track.mtime_ns, "sha256": digest}
                   for track in tracks if (path := track.path) in digests and (digest := digests[path])],
        "spectrum": [{"rel": _relative(path, root), "size": track.size, "mtime_ns": track.mtime_ns,
                      "result": asdict(results[path])} for track in tracks if (path := track.path) in results],
        "errors": report.errors[:10000],
    }
    target = out_dir / EXPORT_NAME
    temporary = target.with_name(target.name + ".tmp")
    with gzip.open(temporary, "wt", encoding="utf-8", compresslevel=6) as stream:
        json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
    os.replace(temporary, target)  # le poste ne lit jamais un fichier à moitié écrit
    report.path, report.seconds = str(target), time.monotonic() - started
    log(f"Résultat : {target} ({report.tracks} pistes, {report.hashed} SHA-256, {report.spectra} spectres, "
        f"{report.seconds:.0f} s).")
    return report


def read_work(path: str | Path) -> dict:
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Fichier de travail lié refusé.")
    with gzip.open(path, "rb") as stream:
        raw = stream.read(MAX_UNCOMPRESSED + 1)
    if len(raw) > MAX_UNCOMPRESSED:
        raise ValueError("Fichier de travail trop volumineux.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("Fichier de travail illisible.") from exc
    if not isinstance(data, dict) or data.get("format") != FORMAT or data.get("version") != VERSION:
        raise ValueError("Ce n'est pas un fichier de travail MusicScope (musicscope-nas.json.gz) compatible.")
    for key in ("tracks", "hashes", "spectrum"):
        if not isinstance(data.get(key), list):
            raise ValueError("Fichier de travail incomplet.")  # noqa: TRY004 (interface : ValueError)
    return data


def _local(local_root: Path, rel) -> Path | None:
    """Chemin sur ce poste ; un chemin absolu ou qui remonte (« .. ») est refusé."""
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel or "\0" in rel:
        return None
    parts = PurePosixPath(rel).parts
    if any(part in ("..", ".", "") for part in parts):
        return None
    return local_root.joinpath(*parts)


def import_work(path: str | Path, local_root: str | Path, cache: Cache, spectrum_cache=None,
                cancel=None, progress=None) -> ImportReport:
    """Reprend dans le cache local les lectures faites sur le NAS, pour les fichiers inchangés."""
    data = read_work(path)
    local_root = Path(local_root).absolute()
    report = ImportReport(root=str(data.get("root", "")), created_at=str(data.get("created_at", "")))
    matched: dict[str, os.stat_result] = {}
    entries = data["tracks"]
    with cache.deferred_commits():
        for position, entry in enumerate(entries, 1):
            check_cancel(cancel)
            if not isinstance(entry, dict):
                continue
            local = _local(local_root, entry.get("rel"))
            if local is None:
                report.changed += 1
                continue
            try:
                info = local.lstat()
            except OSError:
                report.missing += 1
                continue
            if (info.st_size != entry.get("size") or not isinstance(entry.get("mtime_ns"), int)
                    or abs(info.st_mtime_ns - entry["mtime_ns"]) > MTIME_TOLERANCE_NS):
                report.changed += 1  # modifié depuis le passage du NAS : relu au prochain scan
                continue
            try:
                track = Track(str(local), str(entry.get("codec", "")), info.st_size, info.st_mtime_ns,
                              entry.get("duration"), {str(key): [str(value) for value in values]
                                                      for key, values in dict(entry.get("tags") or {}).items()})
            except (TypeError, ValueError, AttributeError):
                report.changed += 1
                continue
            cache.put_track(track, info.st_ctime_ns)
            matched[entry["rel"]] = info
            report.imported += 1
            if progress and position % 500 == 0:
                progress(position, len(entries), f"Import NAS : {local}")
        for entry in data["hashes"]:
            info = matched.get(entry.get("rel")) if isinstance(entry, dict) else None
            if info is None or not isinstance(entry.get("sha256"), str) or len(entry["sha256"]) != 64:
                continue
            cache.connection.execute(
                "INSERT OR REPLACE INTO hashes (path, size, mtime_ns, sha256, ctime_ns) VALUES (?, ?, ?, ?, ?)",
                (str(_local(local_root, entry["rel"])), info.st_size, info.st_mtime_ns, entry["sha256"],
                 info.st_ctime_ns))
            report.hashes += 1
    if spectrum_cache is not None:
        from .spectrum import SpectrumResult
        for entry in data["spectrum"]:
            info = matched.get(entry.get("rel")) if isinstance(entry, dict) else None
            if info is None or not isinstance(entry.get("result"), dict):
                continue
            try:
                result = SpectrumResult(**entry["result"])
            except TypeError:
                continue
            spectrum_cache.put(Track(str(_local(local_root, entry["rel"])), "FLAC", info.st_size, info.st_mtime_ns),
                               result)
            report.spectra += 1
    return report


def main(arguments) -> int:
    """`musicscope --nas-work RACINE --out DOSSIER` : boucle facultative toutes les N heures."""
    while True:
        try:
            run_work(arguments.nas_work, arguments.out, arguments.cache, arguments.spectrum, not arguments.no_hash)
        except Exception as exc:  # noqa: BLE001 — le conteneur reste en vie et réessaiera
            print(f"Travail interrompu : {type(exc).__name__} : {exc}", flush=True)
            if not arguments.every:
                return 1
        if not arguments.every:
            return 0
        print(f"Prochain passage dans {arguments.every:g} h.", flush=True)
        time.sleep(arguments.every * 3600)
