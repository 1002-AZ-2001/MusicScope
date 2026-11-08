"""Emplacements locaux de l'application (jamais dans la médiathèque)."""
import os
import ssl
from pathlib import Path


def default_cache_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / "MusicScope"


def check_cache_outside_library(cache_dir: Path, root: Path) -> None:
    if cache_dir.resolve().is_relative_to(root.resolve()):
        raise ValueError("Le cache doit être situé hors de la médiathèque pour préserver la lecture seule.")


SYSTEM_CA_BUNDLES = (
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/ssl/cert.pem"),
)


def configure_system_trust() -> None:
    """Retrouver les racines système lorsque le binaire change de distribution.

    OpenSSL gelé sur Debian peut chercher /usr/lib/ssl/cert.pem, absent sous
    Arch. Les réglages de confiance fournis par l'utilisateur sont conservés.
    Aucune vérification TLS n'est désactivée et aucun certificat n'est ajouté.
    """
    if "SSL_CERT_FILE" in os.environ or "SSL_CERT_DIR" in os.environ:
        return
    if ssl.get_default_verify_paths().cafile is not None:
        return
    for candidate in SYSTEM_CA_BUNDLES:
        if candidate.is_file():
            try:
                ssl.create_default_context(cafile=str(candidate))
            except (OSError, ssl.SSLError):
                continue
            os.environ["SSL_CERT_FILE"] = str(candidate)
            return
