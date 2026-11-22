"""Mise à jour de MusicScope depuis les versions publiées sur GitHub.

Contrôle : une requête à l'API publique des versions du dépôt (sans compte : 60 requêtes
par heure et par adresse IP), au plus une fois par jour au démarrage, désactivable.

Avant toute installation, le fichier téléchargé doit :
- venir de la dernière version officielle du dépôt (ni brouillon ni préversion), à l'adresse
  de téléchargement de ce dépôt, déposé par le workflow de publication (« github-actions[bot] ») ;
- avoir la taille annoncée, et un SHA-256 identique à la fois à SHA256SUMS et à l'empreinte
  calculée par GitHub (champ ``digest`` de l'API).
La CI publie aussi des attestations Sigstore, vérifiables à la main avec
``gh attestation verify FICHIER -R 1002-AZ-2001/MusicScope``.

Installation : .deb par ``apt-get`` et paquet Arch par ``pacman -U``, lancés via polkit
(``pkexec`` : seule la commande d'installation a les droits d'administration, jamais
l'interface) ; archive portable remplacée sur place, l'ancienne version gardée à côté.
"""
from __future__ import annotations

import hashlib
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import __version__
from .cache import check_cancel

REPOSITORY = "1002-AZ-2001/MusicScope"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_URL = f"https://github.com/{REPOSITORY}/releases"
DOWNLOAD_PREFIX = f"https://github.com/{REPOSITORY}/releases/download/"
PUBLISHER = "github-actions[bot]"
CHECK_INTERVAL = 24 * 3600
MAX_ASSET_BYTES = 512 * 1024 * 1024
MAX_METADATA_BYTES = 2 * 1024 * 1024
OPT_DIR = Path("/opt/musicscope")
INSTALLABLE = ("deb", "arch", "portable")


class UpdateError(RuntimeError):
    """Erreur expliquée à l'utilisateur ; rien n'a été installé."""


def parse_version(text) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v?(\d{1,4})\.(\d{1,4})\.(\d{1,6})", str(text or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def is_newer(candidate: str, current: str = __version__) -> bool:
    new, old = parse_version(candidate), parse_version(current)
    return new is not None and old is not None and new > old


@dataclass(frozen=True)
class Installation:
    kind: str                 # deb, arch, portable, source, unknown
    app_dir: Path | None
    detail: str

    @property
    def can_install(self) -> bool:
        return self.kind in INSTALLABLE


def _lists(path: Path, entry: str) -> bool:
    try:
        return entry in path.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return False


def detect_installation(executable=None, frozen=None, machine=None, opt_dir: Path = OPT_DIR,
                        dpkg_info: Path = Path("/var/lib/dpkg/info"),
                        pacman_local: Path = Path("/var/lib/pacman/local")) -> Installation:
    """Comment cette copie de MusicScope a été installée, donc comment la mettre à jour."""
    frozen = getattr(sys, "frozen", False) if frozen is None else frozen
    if not frozen:
        return Installation("source", None, "Lancé depuis les sources Python : mettez à jour avec git, uv ou pip.")
    if not sys.platform.startswith("linux") or (machine or platform.machine()) not in ("x86_64", "AMD64"):
        return Installation("unknown", None, "Seules les versions Linux x86-64 sont publiées.")
    app_dir = Path(executable or sys.executable).resolve().parent
    if app_dir == opt_dir:
        binary = f"{opt_dir}/MusicScope"
        if _lists(dpkg_info / "musicscope.list", binary):
            return Installation("deb", app_dir, "Paquet Debian : installé avec apt (mot de passe administrateur).")
        for listing in sorted(pacman_local.glob("musicscope-*/files")):
            if _lists(listing, binary.lstrip("/")):
                return Installation("arch", app_dir, "Paquet Arch : installé avec pacman (mot de passe administrateur).")
        return Installation("unknown", app_dir, f"{opt_dir} n'appartient à aucun paquet connu : mettez à jour à la main.")
    if os.access(app_dir, os.W_OK) and os.access(app_dir.parent, os.W_OK):
        return Installation("portable", app_dir, f"Archive portable : remplacée dans {app_dir}, "
                                                 f"l'ancienne version gardée dans {app_dir.name}.precedent.")
    return Installation("unknown", app_dir, f"{app_dir} n'est pas modifiable : mettez à jour à la main.")


def asset_name(kind: str, version: str) -> str:
    return {"deb": f"musicscope_{version}-1_amd64.deb",
            "arch": f"musicscope-{version}-1-x86_64.pkg.tar.zst",
            "portable": f"MusicScope-{version}-linux-x86_64.tar.gz"}[kind]


@dataclass(frozen=True)
class Asset:
    name: str
    size: int
    url: str
    digest: str = ""
    uploader: str = ""
    state: str = ""


@dataclass
class Release:
    version: str
    tag: str
    page: str
    published_at: str = ""
    assets: dict[str, Asset] = field(default_factory=dict)

    @property
    def newer(self) -> bool:
        return is_newer(self.version)


class _HttpsOnly(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise UpdateError("Redirection non chiffrée refusée.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_opener():
    return build_opener(_HttpsOnly()).open


def _request(url: str, accept: str) -> Request:
    return Request(url, headers={"Accept": accept, "User-Agent": f"MusicScope/{__version__} (+{RELEASES_URL})"})


def _explain(exc: Exception) -> UpdateError:
    if isinstance(exc, HTTPError):
        if exc.code in (403, 429):
            return UpdateError("Limite de requêtes GitHub atteinte : réessayez dans une heure.")
        if exc.code == 404:
            return UpdateError("Aucune version publiée trouvée sur GitHub.")
        return UpdateError(f"GitHub a répondu HTTP {exc.code}.")
    if isinstance(exc, URLError):
        return UpdateError(f"GitHub injoignable : {exc.reason}")
    return UpdateError(f"GitHub injoignable : {exc}")


def fetch_latest(opener=None, timeout: float = 15) -> Release:
    """Dernière version publiée (l'API exclut brouillons et préversions)."""
    import json
    opener = opener or default_opener()
    try:
        with opener(_request(API_URL, "application/vnd.github+json"), timeout=timeout) as response:
            raw = response.read(MAX_METADATA_BYTES + 1)
    except UpdateError:
        raise
    except (OSError, ValueError) as exc:
        raise _explain(exc) from exc
    if len(raw) > MAX_METADATA_BYTES:
        raise UpdateError("Réponse de GitHub trop volumineuse.")
    try:
        data = json.loads(raw.decode("utf-8"))
        tag = str(data["tag_name"])
        assets = {}
        for item in data.get("assets") or []:
            asset = Asset(str(item["name"]), int(item["size"]), str(item["browser_download_url"]),
                          str(item.get("digest") or ""), str((item.get("uploader") or {}).get("login", "")),
                          str(item.get("state", "")))
            assets[asset.name] = asset
    except (UnicodeError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise UpdateError("Réponse de GitHub illisible.") from exc
    if data.get("draft") or data.get("prerelease") or parse_version(tag) is None or not tag.startswith("v"):
        raise UpdateError(f"Dernière version GitHub inattendue : {tag}")
    page = str(data.get("html_url") or "")
    if not page.startswith(RELEASES_URL + "/"):
        page = f"{RELEASES_URL}/tag/{tag}"
    return Release(tag[1:], tag, page, str(data.get("published_at") or ""), assets)


def _official(release: Release, name: str) -> Asset:
    asset = release.assets.get(name)
    if asset is None:
        raise UpdateError(f"{name} est absent de la version {release.tag}.")
    if asset.url != f"{DOWNLOAD_PREFIX}{release.tag}/{name}":
        raise UpdateError(f"{name} : adresse de téléchargement inattendue.")
    if asset.uploader != PUBLISHER or asset.state not in ("", "uploaded"):
        raise UpdateError(f"{name} n'a pas été déposé par le workflow de publication : installation refusée.")
    if not 0 < asset.size <= MAX_ASSET_BYTES:
        raise UpdateError(f"{name} : taille annoncée invalide.")
    return asset


def _checksums(text: str) -> dict[str, str]:
    sums = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](\S+)", line.strip())
        if match:
            sums[match.group(2)] = match.group(1)
    return sums


def download(release: Release, installation: Installation, directory: Path, opener=None, cancel=None,
             progress=None, timeout: float = 60) -> Path:
    """Télécharge le fichier de cette installation et le vérifie ; rien n'est gardé s'il diffère."""
    if not installation.can_install:
        raise UpdateError(installation.detail)
    opener = opener or default_opener()
    name = asset_name(installation.kind, release.version)
    asset, sums_asset = _official(release, name), _official(release, "SHA256SUMS")
    try:
        with opener(_request(sums_asset.url, "application/octet-stream"), timeout=timeout) as response:
            sums = _checksums(response.read(64 * 1024).decode("utf-8", errors="replace"))
    except UpdateError:
        raise
    except (OSError, ValueError) as exc:
        raise _explain(exc) from exc
    expected = sums.get(name)
    if expected is None:
        raise UpdateError(f"{name} ne figure pas dans SHA256SUMS : installation refusée.")
    if asset.digest and asset.digest.lower() != f"sha256:{expected}":
        raise UpdateError("SHA256SUMS et l'empreinte calculée par GitHub diffèrent : installation refusée.")
    directory.mkdir(parents=True, exist_ok=True)
    for old in directory.iterdir():  # téléchargements précédents : jamais réutilisés
        if old.is_file() and not old.is_symlink():
            old.unlink()
    target, partial = directory / name, directory / f"{name}.part"
    digest, received = hashlib.sha256(), 0
    try:
        with opener(_request(asset.url, "application/octet-stream"), timeout=timeout) as response, \
                open(partial, "wb") as stream:
            while True:
                check_cancel(cancel)
                block = response.read(1024 * 1024)
                if not block:
                    break
                received += len(block)
                if received > asset.size:
                    raise UpdateError(f"{name} dépasse la taille annoncée : téléchargement interrompu.")
                digest.update(block)
                stream.write(block)
                if progress:
                    progress(received // 1024, asset.size // 1024, f"Téléchargement de {name}")
    except UpdateError:
        partial.unlink(missing_ok=True)
        raise
    except (OSError, ValueError) as exc:
        partial.unlink(missing_ok=True)
        raise _explain(exc) from exc
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    if received != asset.size or digest.hexdigest() != expected:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"{name} : taille ou SHA-256 différent de la version publiée : rien n'est installé.")
    os.replace(partial, target)
    return target


def manual_command(installation: Installation, package: Path) -> str:
    if installation.kind == "deb":
        return shlex.join(["sudo", "apt", "install", str(package)])
    if installation.kind == "arch":
        return shlex.join(["sudo", "pacman", "-U", str(package)])
    return ""


def install_command(installation: Installation, package: Path, which=shutil.which) -> list[str]:
    pkexec = which("pkexec")
    if pkexec is None:
        raise UpdateError("pkexec (polkit) est introuvable. Installez la mise à jour dans un terminal :\n"
                          + manual_command(installation, package))
    if installation.kind == "deb":
        tool = which("apt-get") or "/usr/bin/apt-get"
        return [pkexec, tool, "install", "-y", "--no-install-recommends", str(package)]
    if installation.kind == "arch":
        tool = which("pacman") or "/usr/bin/pacman"
        return [pkexec, tool, "-U", "--noconfirm", str(package)]
    raise UpdateError("Installation par paquet impossible pour cette copie.")


def replace_portable(app_dir: Path, archive: Path) -> Path:
    """Extrait la nouvelle version à côté, puis échange les dossiers ; renvoie l'ancienne version."""
    parent = app_dir.parent
    staging = Path(tempfile.mkdtemp(prefix=".musicscope-maj-", dir=parent))
    try:
        try:
            with tarfile.open(archive, "r:gz") as tar:
                if any(member.name != "MusicScope" and not member.name.startswith("MusicScope/")
                       for member in tar.getmembers()):
                    raise UpdateError("Archive inattendue : tout doit être dans le dossier MusicScope/.")
                tar.extractall(staging, filter="data")  # refuse chemins absolus, « .. » et liens sortants
        except (tarfile.TarError, OSError) as exc:
            raise UpdateError(f"Archive illisible : {exc}") from exc
        new = staging / "MusicScope"
        binary = new / "MusicScope"
        if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
            raise UpdateError("Archive incomplète : exécutable MusicScope absent.")
        previous = parent / f"{app_dir.name}.precedent"
        if previous.exists() or previous.is_symlink():
            if previous.is_symlink() or not (previous / "MusicScope").is_file():
                raise UpdateError(f"{previous} existe et n'est pas une version de MusicScope : retirez-le.")
            shutil.rmtree(previous)
        os.rename(app_dir, previous)
        try:
            os.rename(new, app_dir)
        except OSError:
            os.rename(previous, app_dir)
            raise
        return previous
    finally:
        shutil.rmtree(staging, ignore_errors=True)


@dataclass
class InstallResult:
    restart: list[str]
    message: str


def install(installation: Installation, package: Path, runner=subprocess.run, which=shutil.which) -> InstallResult:
    if installation.kind == "portable":
        previous = replace_portable(installation.app_dir, package)
        return InstallResult([str(installation.app_dir / "MusicScope")],
                             f"Nouvelle version installée ; l'ancienne reste dans {previous}.")
    command = install_command(installation, package, which)
    try:
        done = runner(command, capture_output=True, text=True, timeout=1800, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise UpdateError(f"Installation impossible : {exc}") from exc
    if done.returncode in (126, 127):  # pkexec : fenêtre fermée, ou autorisation refusée
        raise UpdateError("Autorisation administrateur refusée ou annulée : rien n'a été installé.")
    if done.returncode != 0:
        tail = "\n".join((done.stderr or done.stdout or "").strip().splitlines()[-5:])
        raise UpdateError(f"L'installation a échoué (code {done.returncode}) :\n{tail}")
    launcher = Path("/usr/bin/musicscope")
    return InstallResult([str(launcher) if launcher.exists() else str(OPT_DIR / "MusicScope")],
                         "Nouvelle version installée.")


def due(last_check, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    return not isinstance(last_check, (int, float)) or not 0 <= now - last_check < CHECK_INTERVAL
