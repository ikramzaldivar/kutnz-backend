"""Generación de códigos cortos y validación de alias.

Los códigos son insensibles a mayúsculas: todo se guarda en minúsculas, así un alias
("Mi-CV" == "mi-cv") y un código aleatorio comparten el mismo espacio sin ambigüedad.
El alfabeto excluye 0/o y 1/l/i para que un enlace impreso se pueda teclear sin errores.
"""
import re
import secrets

ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"  # 31 símbolos
CODE_LENGTH = 7  # 31^7 ≈ 2.75e10 códigos posibles

ALIAS_MIN, ALIAS_MAX = 3, 32
_ALIAS_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")

RESERVED = frozenset({
    "api", "admin", "app", "assets", "auth", "callback", "dashboard", "docs", "favicon.ico",
    "health", "healthz", "help", "kut", "kutnz", "login", "logout", "metrics", "openapi.json",
    "panel", "preview", "privacy", "qr", "redoc", "register", "report", "robots.txt", "settings",
    "signup", "static", "status", "terms", "www",
})


def generate_code() -> str:
    """Genera un código aleatorio de CODE_LENGTH símbolos con un RNG criptográfico."""
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def normalize(code: str) -> str:
    """Quita espacios y pasa a minúsculas; es la forma canónica en la que se guardan los códigos."""
    return code.strip().lower()


class AliasError(ValueError):
    """Alias inválido; el mensaje se muestra tal cual al usuario."""


def validate_alias(alias: str) -> str:
    """Normaliza el alias y lo valida (longitud, caracteres, guiones y reservados).

    Devuelve el alias normalizado o lanza AliasError con un mensaje para el usuario.
    """
    a = normalize(alias)
    if not ALIAS_MIN <= len(a) <= ALIAS_MAX:
        raise AliasError(f"El alias debe tener entre {ALIAS_MIN} y {ALIAS_MAX} caracteres.")
    if not _ALIAS_RE.match(a):
        raise AliasError("Solo letras, números y guiones; no puede empezar ni terminar con guion.")
    if "--" in a:
        raise AliasError("El alias no puede tener guiones consecutivos.")
    if a in RESERVED:
        raise AliasError("Ese alias está reservado por el sistema.")
    return a
