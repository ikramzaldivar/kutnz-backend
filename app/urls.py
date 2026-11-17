"""Validación de URLs de destino."""
from urllib.parse import urlsplit

MAX_URL_LENGTH = 2048


class UrlError(ValueError):
    pass


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".")


def validate_target(url: str, self_hosts: set[str]) -> str:
    url = url.strip()
    if not url or len(url) > MAX_URL_LENGTH:
        raise UrlError(f"La URL debe tener entre 1 y {MAX_URL_LENGTH} caracteres.")
    try:
        parts = urlsplit(url)
        port = parts.port  # lanza ValueError si el puerto es inválido
    except ValueError as exc:
        raise UrlError("La URL no está bien formada.") from exc
    if parts.scheme.lower() not in ("http", "https"):
        raise UrlError("Solo se permiten URLs http o https.")
    host = (parts.hostname or "").rstrip(".")
    if not host or " " in url:
        raise UrlError("La URL no está bien formada.")
    if "." not in host and host != "localhost":
        raise UrlError("El dominio de la URL no es válido.")
    if parts.username or parts.password:
        raise UrlError("La URL no puede incluir usuario o contraseña.")
    host = host.lower()
    if host in self_hosts or any(host.endswith("." + h) for h in self_hosts):
        raise UrlError("No se puede acortar un enlace de kut.nz (evita bucles de redirección).")
    del port
    return url
