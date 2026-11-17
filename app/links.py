"""Reglas de negocio de los enlaces (independientes de HTTP)."""
import time
from datetime import datetime, timezone

from . import cache, codes, security, store
from .config import get_settings
from .urls import validate_target

MAX_CLICKS_LIMIT = 1_000_000_000
CODE_RETRIES = 5


class InvalidInput(ValueError):
    pass


class Forbidden(Exception):
    pass


def now() -> int:
    return int(time.time())


def parse_expiry(value: str | int | None) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, int):
        ts = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise InvalidInput("Fecha de caducidad inválida (usa ISO 8601).") from exc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = int(dt.timestamp())
    if ts <= now():
        raise InvalidInput("La fecha de caducidad debe estar en el futuro.")
    return ts


def parse_max_clicks(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        n = int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidInput("El límite de clics debe ser un número entero.") from exc
    if not 1 <= n <= MAX_CLICKS_LIMIT:
        raise InvalidInput("El límite de clics debe ser al menos 1.")
    return n


def status_of(link: dict, t: int | None = None) -> str:
    t = t or now()
    if link.get("disabled"):
        return "desactivado"
    if link.get("expires_at") and link["expires_at"] <= t:
        return "caducado"
    if link.get("max_clicks") and link.get("clicks", 0) >= link["max_clicks"]:
        return "agotado"
    return "activo"


def short_url(code: str) -> str:
    return f"{get_settings().base_url}/{code}"


def public_view(link: dict) -> dict:
    """Representación para la API (nunca expone el hash de la contraseña)."""
    return {
        "code": link["code"],
        "short_url": short_url(link["code"]),
        "target": link["target"],
        "title": link.get("title"),
        "created_at": link["created_at"],
        "updated_at": link.get("updated_at"),
        "expires_at": link.get("expires_at"),
        "max_clicks": link.get("max_clicks"),
        "clicks": link.get("clicks", 0),
        "has_password": bool(link.get("password_hash")),
        "disabled": bool(link.get("disabled")),
        "disabled_by_admin": bool(link.get("disabled_by_admin")),
        "is_alias": bool(link.get("is_alias")),
        "status": status_of(link),
        "qr_png": f"/api/v1/links/{link['code']}/qr.png",
        "qr_svg": f"/api/v1/links/{link['code']}/qr.svg",
    }


def cache_view(link: dict) -> dict:
    """Lo mínimo que necesita la redirección; se guarda en Redis."""
    return {
        "code": link["code"],
        "target": link["target"],
        "disabled": bool(link.get("disabled")),
        "expires_at": link.get("expires_at"),
        "max_clicks": link.get("max_clicks"),
        "clicks": link.get("clicks", 0),
        "has_password": bool(link.get("password_hash")),
        "owner_id": link.get("owner_id"),
    }


def create(data: dict, owner_id: str | None) -> dict:
    s = get_settings()
    try:
        target = validate_target(data.get("url", ""), s.self_hosts)
    except ValueError as exc:
        raise InvalidInput(str(exc)) from exc

    advanced = {k: data.get(k) for k in ("alias", "expires_at", "max_clicks", "password") if data.get(k) not in (None, "")}
    if advanced and owner_id is None:
        raise Forbidden("Alias, caducidad, límite de clics y contraseña requieren una cuenta.")

    try:
        security.check_target(target)
    except security.UnsafeTarget as exc:
        raise InvalidInput(str(exc)) from exc

    item = {"target": target, "created_at": now(), "clicks": 0}
    if owner_id:
        item["owner_id"] = owner_id
    if data.get("title"):
        item["title"] = str(data["title"])[:120]
    if (exp := parse_expiry(advanced.get("expires_at"))) is not None:
        item["expires_at"] = exp
        item["ttl"] = exp + 90 * 86400  # DynamoDB borra el registro 90 días después de caducar
    if (mc := parse_max_clicks(advanced.get("max_clicks"))) is not None:
        item["max_clicks"] = mc
    if advanced.get("password"):
        try:
            item["password_hash"] = security.hash_password(str(advanced["password"]))
        except ValueError as exc:
            raise InvalidInput(str(exc)) from exc

    if advanced.get("alias"):
        try:
            item["code"] = codes.validate_alias(str(advanced["alias"]))
        except codes.AliasError as exc:
            raise InvalidInput(str(exc)) from exc
        item["is_alias"] = True
        try:
            link = store.create_link(item)
        except store.Conflict as exc:
            raise InvalidInput("Ese alias ya está en uso.") from exc
    else:
        for _ in range(CODE_RETRIES):
            item["code"] = codes.generate_code()
            try:
                link = store.create_link(item)
                break
            except store.Conflict:
                continue
        else:
            raise RuntimeError("No se pudo generar un código único.")

    # Lectura de lo propio inmediatamente después de crear: dejamos la vista en caché.
    cache.put_link(link["code"], cache_view(link))
    return link


def get_owned(code: str, user) -> dict:
    link = store.get_link(codes.normalize(code), consistent=True)
    if not link or (link.get("owner_id") != user.id and not user.is_admin):
        raise store.NotFound(code)  # mismo error para "no existe" y "no es tuyo": no revela qué códigos existen
    return link


def update(code: str, data: dict, user) -> dict:
    link = get_owned(code, user)
    changes: dict = {}
    if "url" in data and data["url"] is not None:
        try:
            changes["target"] = validate_target(data["url"], get_settings().self_hosts)
            security.check_target(changes["target"])
        except (ValueError, security.UnsafeTarget) as exc:
            raise InvalidInput(str(exc)) from exc
    if "title" in data:
        changes["title"] = (str(data["title"])[:120] or None) if data["title"] else None
    if "expires_at" in data:
        exp = parse_expiry(data["expires_at"])
        changes["expires_at"] = exp
        changes["ttl"] = exp + 90 * 86400 if exp else None
    if "max_clicks" in data:
        mc = parse_max_clicks(data["max_clicks"])
        if mc is not None and mc < link.get("clicks", 0):
            raise InvalidInput("El límite no puede ser menor que los clics ya registrados.")
        changes["max_clicks"] = mc
    if "password" in data:
        if data["password"]:
            try:
                changes["password_hash"] = security.hash_password(str(data["password"]))
            except ValueError as exc:
                raise InvalidInput(str(exc)) from exc
        else:
            changes["password_hash"] = None
    if "disabled" in data:
        if link.get("disabled_by_admin") and not user.is_admin:
            raise Forbidden("Este enlace fue desactivado por un administrador.")
        changes["disabled"] = bool(data["disabled"])
        if user.is_admin:
            changes["disabled_by_admin"] = bool(data["disabled"]) or None
    if not changes:
        return link
    owner = None if user.is_admin else user.id
    updated = store.update_link(link["code"], changes, owner)
    cache.invalidate(link["code"])  # el cambio se refleja en la siguiente visita
    return updated


def delete(code: str, user) -> None:
    link = get_owned(code, user)
    store.delete_link(link["code"], None if user.is_admin else user.id)
    cache.invalidate(link["code"])
    store.delete_stats(link["code"])


def list_for(user, q: str = "", sort: str = "-created_at", limit: int = 1000) -> list[dict]:
    items, cursor = [], None
    while len(items) < limit:
        page, cursor = store.list_links(user.id, limit=100, cursor=cursor)
        items += page
        if not cursor:
            break
    views = [public_view(i) for i in items]
    if q:
        ql = q.lower()
        views = [v for v in views if ql in v["code"] or ql in v["target"].lower() or ql in (v["title"] or "").lower()]
    key, reverse = sort.lstrip("-"), sort.startswith("-")
    if key not in ("created_at", "clicks", "code", "target", "status"):
        key = "created_at"
    views.sort(key=lambda v: (v[key] is None, v[key] or 0) if key in ("created_at", "clicks") else str(v[key]),
               reverse=reverse)
    return views
