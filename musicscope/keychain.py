"""Clés des services dans le trousseau du système, si l'utilisateur le demande.

Le trousseau est celui du bureau, via l'API Secret Service de freedesktop.org (GNOME Keyring,
KeePassXC, ou KDE quand son service Secret Service est actif) ; sous Windows, le gestionnaire
d'identification. Rien n'est enregistré sans la case « Mémoriser dans le trousseau » ; sans
trousseau disponible, les clés restent en mémoire pour la session, comme avant.

Les préférences (JSON du cache) ne contiennent jamais de clé : seulement l'indication qu'une
clé a été mémorisée pour tel service et telle adresse.
"""
from __future__ import annotations

import os
import sys

SERVICE = "MusicScope"


def account(service: str, server: str = "", user: str = "") -> str:
    """Nom de l'entrée : service, adresse normalisée et, s'il y a lieu, identifiant."""
    parts = [service, server.strip().rstrip("/").lower()]
    if user:
        parts.append(user.strip())
    return ":".join(part for part in parts if part) if server else service


def _system_backend():
    """(trousseau, raison) ; trousseau None si aucun n'est utilisable ici."""
    if os.environ.get("MUSICSCOPE_KEYRING", "").lower() in ("none", "off", "0"):
        return None, "Trousseau désactivé (MUSICSCOPE_KEYRING)."
    try:
        if sys.platform.startswith("linux"):
            from keyring.backends import SecretService
            backend = SecretService.Keyring()
        elif sys.platform == "win32":
            from keyring.backends import Windows
            backend = Windows.WinVaultKeyring()
        elif sys.platform == "darwin":
            from keyring.backends import macOS
            backend = macOS.Keyring()
        else:
            return None, "Aucun trousseau pris en charge sur ce système."
        type(backend).priority  # noqa: B018 — lève une erreur si le service est absent
    except Exception as exc:  # noqa: BLE001 — bibliothèque absente, D-Bus absent, service arrêté…
        return None, f"Trousseau du système indisponible : {exc}"
    if hasattr(backend, "appid"):
        backend.appid = SERVICE  # libellé affiché dans le gestionnaire de mots de passe
    return backend, ""


class Keychain:
    """Accès tolérant aux erreurs : un trousseau verrouillé ou refusé ne bloque jamais l'application."""

    def __init__(self, backend="auto"):
        self._backend, self.reason = (_system_backend() if backend == "auto" else (backend, ""))
        if self._backend is None and not self.reason:
            self.reason = "Trousseau du système indisponible."

    @property
    def available(self) -> bool:
        return self._backend is not None

    def get(self, name: str) -> str:
        if self._backend is None:
            return ""
        try:
            return self._backend.get_password(SERVICE, name) or ""
        except Exception as exc:  # noqa: BLE001 — trousseau verrouillé, déverrouillage refusé…
            self.reason = f"Lecture du trousseau impossible : {exc}"
            return ""

    def set(self, name: str, value: str) -> bool:
        if self._backend is None or not value:
            return False
        try:
            self._backend.set_password(SERVICE, name, value)
        except Exception as exc:  # noqa: BLE001
            self.reason = f"Écriture dans le trousseau impossible : {exc}"
            return False
        return True

    def delete(self, name: str) -> bool:
        if self._backend is None:
            return False
        try:
            self._backend.delete_password(SERVICE, name)
        except Exception:  # noqa: BLE001 — absente : rien à oublier
            return False
        return True


def keychain_for(window) -> Keychain:
    """Un seul accès au trousseau par fenêtre, ouvert à la première utilisation."""
    keychain = getattr(window, "keychain", None)
    if keychain is None:
        keychain = window.keychain = Keychain()
    return keychain


def remembered(window, service: str) -> bool:
    saved = window._preferences().get("remembered") or {}
    return isinstance(saved, dict) and bool(saved.get(service))


def remember(window, service: str, secrets: dict[str, str], keep: bool) -> str:
    """Mémorise (`keep`) ou oublie les `secrets` {entrée: valeur} d'un service ; renvoie un message."""
    keychain = keychain_for(window)
    saved = dict(window._preferences().get("remembered") or {})
    if not keep:
        for name in secrets:
            keychain.delete(name)
        if saved.pop(service, None) is not None:
            window.save_preferences(remembered=saved)
        return ""
    if not keychain.available:
        return f"Clé non mémorisée : {keychain.reason}"
    stored = [keychain.set(name, value) for name, value in secrets.items() if value]
    for name, value in secrets.items():
        if not value:
            keychain.delete(name)  # champ vidé : l'ancienne valeur ne doit pas resurgir
    if stored and all(stored):
        saved[service] = True
        window.save_preferences(remembered=saved)
        return "Clé mémorisée dans le trousseau du système."
    return f"Clé non mémorisée : {keychain.reason}" if stored else ""


def recall(window, service: str, names) -> dict[str, str]:
    """Valeurs mémorisées, lues seulement si l'utilisateur a choisi de mémoriser ce service."""
    if not remembered(window, service):
        return {}
    keychain = keychain_for(window)
    return {name: value for name in names if (value := keychain.get(name))}
