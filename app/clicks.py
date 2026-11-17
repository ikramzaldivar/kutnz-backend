"""Construcción y publicación asíncrona de eventos de clic.

No se guarda la IP: solo el país (cabecera de CloudFront) y un identificador anónimo
derivado con una sal que cambia cada día, suficiente para estimar visitantes únicos.
"""
import hashlib
import hmac
import json
import logging
import re
import time
from datetime import datetime, timezone
from functools import lru_cache
from urllib.parse import urlsplit

import boto3
from user_agents import parse as parse_ua

from .config import get_settings

log = logging.getLogger("kutnz.clicks")

_BOT_RE = re.compile(
    r"bot|crawl|spider|slurp|facebookexternalhit|whatsapp|telegram|discord|preview|embedly|"
    r"headless|python-requests|curl/|wget|httpclient|okhttp|go-http|monitor|uptime",
    re.I,
)


def is_bot(user_agent: str) -> bool:
    return not user_agent or bool(_BOT_RE.search(user_agent))


def visitor_id(ip: str, user_agent: str, day: str) -> str:
    secret = get_settings().visitor_salt_secret.encode()
    daily_salt = hmac.new(secret, day.encode(), hashlib.sha256).digest()
    return hmac.new(daily_salt, f"{ip}|{user_agent}".encode(), hashlib.sha256).hexdigest()[:16]


def build_event(code: str, ip: str, user_agent: str, referer: str, country: str, counted: bool) -> dict:
    now = time.time()
    day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    ua = parse_ua(user_agent or "")
    device = "mobile" if ua.is_mobile else "tablet" if ua.is_tablet else "desktop" if ua.is_pc else "other"
    ref_host = (urlsplit(referer).hostname or "").removeprefix("www.") if referer else ""
    return {
        "code": code,
        "ts": int(now),
        "day": day,
        "country": (country or "??").upper()[:2],
        "device": device,
        "browser": ua.browser.family or "Other",
        "os": ua.os.family or "Other",
        "referer": ref_host or "directo",
        "visitor": visitor_id(ip, user_agent, day),
        "counted": counted,  # True si el clic ya se sumó de forma síncrona (enlaces con límite)
    }


@lru_cache
def _sqs():
    s = get_settings()
    return boto3.client("sqs", region_name=s.aws_region, endpoint_url=s.aws_endpoint_url)


def publish(event: dict) -> None:
    url = get_settings().clicks_queue_url
    if not url:
        return
    try:
        _sqs().send_message(QueueUrl=url, MessageBody=json.dumps(event))
    except Exception as exc:  # una métrica perdida no debe tumbar la redirección
        log.warning("no se pudo publicar el clic: %s", exc)
