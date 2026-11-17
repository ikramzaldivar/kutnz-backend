"""Contraseñas de enlaces (argon2) y revisión de destinos (Safe Browsing + lista local)."""
import logging

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from . import store
from .config import get_settings
from .urls import host_of

log = logging.getLogger("kutnz.security")
_ph = PasswordHasher()

PASSWORD_MIN, PASSWORD_MAX = 4, 128


def hash_password(password: str) -> str:
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise ValueError(f"La contraseña debe tener entre {PASSWORD_MIN} y {PASSWORD_MAX} caracteres.")
    return _ph.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerificationError:
        return False


class UnsafeTarget(ValueError):
    pass


SAFE_BROWSING_URL = "https://safebrowsing.googleapis.com/v4/threatMatches:find"


def _domain_blocked(host: str) -> bool:
    parts = host.split(".")
    # bloquea el dominio y sus subdominios: a.b.evil.com coincide con evil.com
    return any(store.moderation_has("domain", ".".join(parts[i:])) for i in range(len(parts) - 1))


def check_target(url: str) -> None:
    host = host_of(url)
    if _domain_blocked(host):
        raise UnsafeTarget("El dominio de destino está bloqueado.")
    key = get_settings().safe_browsing_api_key
    if not key:
        return
    body = {
        "client": {"clientId": "kutnz", "clientVersion": "1.0"},
        "threatInfo": {
            "threatTypes": ["MALWARE", "SOCIAL_ENGINEERING", "UNWANTED_SOFTWARE", "POTENTIALLY_HARMFUL_APPLICATION"],
            "platformTypes": ["ANY_PLATFORM"],
            "threatEntryTypes": ["URL"],
            "threatEntries": [{"url": url}],
        },
    }
    try:
        resp = httpx.post(SAFE_BROWSING_URL, params={"key": key}, json=body, timeout=2.0)
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        # Si Google no responde, no bloqueamos la creación (fail-open) pero queda registrado.
        log.warning("Safe Browsing no disponible: %s", exc)
        return
    if resp.json().get("matches"):
        raise UnsafeTarget("Google Safe Browsing marcó este destino como peligroso.")
