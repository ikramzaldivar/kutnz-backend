"""Redis: caché de redirección, rate limiting e idempotencia.

Si Redis no responde, la app sigue funcionando contra DynamoDB (fail-open): la caché
mejora la latencia pero no es parte de la disponibilidad del servicio.
"""
import json
import logging
import random
import time
from functools import lru_cache

import redis

from .config import get_settings

log = logging.getLogger("kutnz.cache")
NOT_FOUND = "__404__"

# Circuit breaker: tras un fallo de Redis dejamos de intentarlo unos segundos y vamos directo a DynamoDB.
# Sin esto, durante un failover cada petición esperaba el timeout de conexión (lo medimos: ≈ 30 s de p95 > 1 s).
BREAKER_SECONDS = 5.0
_open_until = 0.0


@lru_cache
def client() -> redis.Redis:
    # Timeouts cortos: si Redis tarda, es mejor ir directo a DynamoDB que hacer esperar al visitante.
    return redis.Redis.from_url(get_settings().redis_url, socket_timeout=0.25, socket_connect_timeout=0.25,
                                health_check_interval=30, decode_responses=True)


def _safe(fn, default=None):
    global _open_until
    if time.monotonic() < _open_until:
        return default
    try:
        return fn()
    except redis.RedisError as exc:
        _open_until = time.monotonic() + BREAKER_SECONDS
        log.warning("redis no disponible, circuito abierto %.0f s: %s", BREAKER_SECONDS, exc)
        return default


def _key(code: str) -> str:
    return f"link:{code}"


def get_link(code: str):
    """Devuelve dict, NOT_FOUND (caché negativa) o None (no está en caché)."""
    raw = _safe(lambda: client().get(_key(code)))
    if raw is None:
        return None
    return NOT_FOUND if raw == NOT_FOUND else json.loads(raw)


def put_link(code: str, view: dict) -> None:
    s = get_settings()
    ttl = s.cache_ttl_seconds + random.randint(0, s.cache_ttl_seconds // 5)  # TTL escalonado: evita avalanche
    _safe(lambda: client().set(_key(code), json.dumps(view), ex=ttl))


def put_not_found(code: str) -> None:
    _safe(lambda: client().set(_key(code), NOT_FOUND, ex=get_settings().negative_cache_ttl_seconds))


def invalidate(code: str) -> None:
    _safe(lambda: client().delete(_key(code)))


def hit(bucket: str, limit: int, window_seconds: int) -> bool:
    """Ventana fija. True si la petición está permitida."""
    key = f"rl:{bucket}:{int(time.time()) // window_seconds}"

    def _do():
        pipe = client().pipeline()
        pipe.incr(key)
        pipe.expire(key, window_seconds)
        count, _ = pipe.execute()
        return count <= limit

    return _safe(_do, default=True)


def idempotency_get(key: str):
    raw = _safe(lambda: client().get(f"idem:{key}"))
    return json.loads(raw) if raw else None


def idempotency_put(key: str, value: dict, ttl: int = 86400) -> None:
    _safe(lambda: client().set(f"idem:{key}", json.dumps(value), ex=ttl))


def ping() -> bool:
    return bool(_safe(lambda: client().ping(), default=False))
