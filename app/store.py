"""Acceso a DynamoDB.

Tablas:
- links:       PK code. GSI "owner-index" (owner_id, created_at) para el panel.
- stats:       PK code, SK dimensión ("day#2026-11-16", "country#MX", "uv#2026-11-16"...).
- moderation:  PK kind ("domain" | "user" | "report"), SK valor.
"""
import base64
import json
import time
from decimal import Decimal
from functools import lru_cache

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from .config import get_settings


class Conflict(Exception):
    """El código ya existe (la escritura condicional falló)."""


class NotFound(Exception):
    """El enlace no existe o no pertenece al usuario."""


@lru_cache
def _ddb():
    """Recurso de DynamoDB compartido; aws_endpoint_url apunta a DynamoDB Local en desarrollo."""
    s = get_settings()
    return boto3.resource("dynamodb", region_name=s.aws_region, endpoint_url=s.aws_endpoint_url)


def _table(name: str):
    return _ddb().Table(name)


def links_table():
    return _table(get_settings().table_links)


def stats_table():
    return _table(get_settings().table_stats)


def moderation_table():
    return _table(get_settings().table_moderation)


def _plain(item: dict | None) -> dict | None:
    """Convierte Decimal → int para serializar a JSON."""
    if item is None:
        return None
    out = {}
    for k, v in item.items():
        if isinstance(v, Decimal):
            out[k] = int(v)
        elif isinstance(v, set):
            out[k] = sorted(v)
        else:
            out[k] = v
    return out


def _is_conditional_failure(exc: ClientError) -> bool:
    """True si DynamoDB rechazó la escritura porque no se cumplió la ConditionExpression."""
    return exc.response["Error"]["Code"] == "ConditionalCheckFailedException"


# ---------------- enlaces ----------------

def create_link(item: dict) -> dict:
    """Inserta el enlace solo si el código está libre; si no, lanza Conflict."""
    try:
        links_table().put_item(Item=item, ConditionExpression=Attr("code").not_exists())
    except ClientError as exc:
        if _is_conditional_failure(exc):
            raise Conflict(item["code"]) from exc
        raise
    return _plain(item)


def get_link(code: str, consistent: bool = False) -> dict | None:
    """Lee un enlace por código. consistent=True evita leer una versión vieja tras una escritura."""
    resp = links_table().get_item(Key={"code": code}, ConsistentRead=consistent)
    return _plain(resp.get("Item"))


_UPDATABLE = {"target", "expires_at", "ttl", "max_clicks", "password_hash", "disabled", "title", "disabled_by_admin"}


def update_link(code: str, changes: dict, owner_id: str | None) -> dict:
    """Actualiza campos. Si owner_id no es None, solo el dueño puede hacerlo (condición atómica)."""
    # Prefijos propios (#upd/:upd): boto3 genera #n0/:v0 para la ConditionExpression y chocarían.
    sets, removes, values, names = [], [], {":updnow": int(time.time())}, {}
    for i, (field, value) in enumerate(changes.items()):
        if field not in _UPDATABLE:
            raise ValueError(field)
        names[f"#upd{i}"] = field
        if value is None:
            removes.append(f"#upd{i}")
        else:
            sets.append(f"#upd{i} = :upd{i}")
            values[f":upd{i}"] = value
    sets.append("updated_at = :updnow")
    expr = "SET " + ", ".join(sets) + (" REMOVE " + ", ".join(removes) if removes else "")
    cond = Attr("code").exists()
    if owner_id is not None:
        cond = cond & Attr("owner_id").eq(owner_id)
    kwargs = {"ExpressionAttributeNames": names} if names else {}
    try:
        resp = links_table().update_item(
            Key={"code": code}, UpdateExpression=expr, ConditionExpression=cond,
            ExpressionAttributeValues=values, ReturnValues="ALL_NEW", **kwargs,
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            raise NotFound(code) from exc
        raise
    return _plain(resp["Attributes"])


def delete_link(code: str, owner_id: str | None) -> None:
    """Borra el enlace. Con owner_id, solo si es del dueño; si no, lanza NotFound."""
    cond = Attr("code").exists()
    if owner_id is not None:
        cond = cond & Attr("owner_id").eq(owner_id)
    try:
        links_table().delete_item(Key={"code": code}, ConditionExpression=cond)
    except ClientError as exc:
        if _is_conditional_failure(exc):
            raise NotFound(code) from exc
        raise


def list_links(owner_id: str, limit: int = 50, cursor: str | None = None) -> tuple[list[dict], str | None]:
    """Enlaces del usuario, del más nuevo al más viejo. El cursor es la LastEvaluatedKey en base64."""
    kwargs = {
        "IndexName": "owner-index",
        "KeyConditionExpression": Key("owner_id").eq(owner_id),
        "ScanIndexForward": False,
        "Limit": limit,
    }
    if cursor:
        kwargs["ExclusiveStartKey"] = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    resp = links_table().query(**kwargs)
    nxt = resp.get("LastEvaluatedKey")
    token = base64.urlsafe_b64encode(json.dumps(_plain(nxt)).encode()).decode() if nxt else None
    return [_plain(i) for i in resp["Items"]], token


def consume_limited_click(code: str, now: int) -> bool:
    """Suma un clic solo si quedan clics disponibles. Atómico en DynamoDB: aunque lleguen
    1 000 visitas simultáneas a un enlace con máximo 100, exactamente 100 pasan."""
    try:
        links_table().update_item(
            Key={"code": code},
            UpdateExpression="SET clicks = if_not_exists(clicks, :zero) + :one",
            ConditionExpression=(
                "attribute_exists(max_clicks) AND (attribute_not_exists(clicks) OR clicks < max_clicks)"
                " AND (attribute_not_exists(disabled) OR disabled = :false)"
                " AND (attribute_not_exists(expires_at) OR expires_at > :now)"
            ),
            ExpressionAttributeValues={":zero": 0, ":one": 1, ":false": False, ":now": now},
        )
        return True
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return False
        raise


def add_clicks(code: str, n: int) -> None:
    """Conteo eventual para enlaces sin límite (lo llama el worker)."""
    try:
        links_table().update_item(
            Key={"code": code}, UpdateExpression="ADD clicks :n",
            ConditionExpression=Attr("code").exists(), ExpressionAttributeValues={":n": n},
        )
    except ClientError as exc:
        if not _is_conditional_failure(exc):  # el enlace pudo borrarse mientras tanto
            raise


# ---------------- métricas ----------------

def add_stat(code: str, dimension: str, n: int = 1) -> None:
    """Suma n al contador de una dimensión ("day#...", "country#...", etc.)."""
    stats_table().update_item(
        Key={"code": code, "dim": dimension}, UpdateExpression="ADD n :n", ExpressionAttributeValues={":n": n},
    )


def add_unique_visitors(code: str, day: str, visitors: set[str]) -> None:
    """Agrega identificadores anónimos al conjunto de visitantes del día."""
    stats_table().update_item(
        Key={"code": code, "dim": f"uv#{day}"}, UpdateExpression="ADD visitors :v",
        ExpressionAttributeValues={":v": set(visitors)},
    )


def get_stats(code: str) -> list[dict]:
    """Todas las dimensiones de un enlace, recorriendo todas las páginas de la consulta."""
    items, kwargs = [], {"KeyConditionExpression": Key("code").eq(code)}
    while True:
        resp = stats_table().query(**kwargs)
        items += [_plain(i) for i in resp["Items"]]
        if "LastEvaluatedKey" not in resp:
            return items
        kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def delete_stats(code: str) -> None:
    """Borra las métricas de un enlace (se llama al eliminarlo)."""
    with stats_table().batch_writer() as batch:
        for item in get_stats(code):
            batch.delete_item(Key={"code": code, "dim": item["dim"]})


# ---------------- moderación ----------------

def moderation_put(kind: str, value: str, extra: dict | None = None) -> None:
    """Agrega un dominio, usuario o reporte a la tabla de moderación."""
    moderation_table().put_item(Item={"kind": kind, "value": value, "created_at": int(time.time()), **(extra or {})})


def moderation_delete(kind: str, value: str) -> None:
    moderation_table().delete_item(Key={"kind": kind, "value": value})


def moderation_has(kind: str, value: str) -> bool:
    """True si el valor está registrado (p. ej. un dominio bloqueado)."""
    return "Item" in moderation_table().get_item(Key={"kind": kind, "value": value})


def moderation_list(kind: str, limit: int = 100) -> list[dict]:
    resp = moderation_table().query(KeyConditionExpression=Key("kind").eq(kind), Limit=limit, ScanIndexForward=False)
    return [_plain(i) for i in resp["Items"]]
