"""Portabilité des racines TLS Debian/Arch sans contourner leur vérification."""
import os
import ssl
from pathlib import Path
from types import SimpleNamespace

from musicscope.paths import SYSTEM_CA_BUNDLES, configure_system_trust


def test_frozen_default_missing_uses_verified_system_ca(monkeypatch):
    # C'est précisément le cas à vérifier : le Python du runner peut chercher
    # ailleurs, alors que la distribution possède des racines valides.
    bundle = next((path for path in SYSTEM_CA_BUNDLES if path.is_file()), None)
    assert bundle is not None, "La distribution doit fournir ses certificats système."
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr("musicscope.paths.ssl.get_default_verify_paths", lambda: SimpleNamespace(cafile=None))
    monkeypatch.setattr("musicscope.paths.SYSTEM_CA_BUNDLES", (bundle,))
    configure_system_trust()
    assert os.environ["SSL_CERT_FILE"] == str(bundle)
    context = ssl.create_default_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.get_ca_certs()


def test_custom_cloud_or_user_trust_remains_unchanged(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/custom/ca-bundle.pem")
    configure_system_trust()
    assert os.environ["SSL_CERT_FILE"] == "/custom/ca-bundle.pem"


def test_existing_native_system_trust_needs_no_override(monkeypatch):
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr("musicscope.paths.ssl.get_default_verify_paths", lambda: SimpleNamespace(cafile="/native/system.pem"))
    configure_system_trust()
    assert "SSL_CERT_FILE" not in os.environ
