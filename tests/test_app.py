import json
import time
from concurrent.futures import ThreadPoolExecutor

import boto3
import pytest

from app import codes
from app.urls import UrlError, validate_target
from tests.conftest import auth

BROWSER = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/130.0 Safari/537.36"}


# ---------- unidades ----------

def test_codes_have_no_ambiguous_chars():
    for _ in range(200):
        c = codes.generate_code()
        assert len(c) == 7 and not set(c) & set("01ilo")


@pytest.mark.parametrize("alias,ok", [("mi-cv", True), ("Mi-CV", True), ("ab", False), ("-x-", False),
                                      ("a--b", False), ("api", False), ("panel", False), ("hola mundo", False)])
def test_alias_rules(alias, ok):
    if ok:
        assert codes.validate_alias(alias) == alias.lower()
    else:
        with pytest.raises(codes.AliasError):
            codes.validate_alias(alias)


@pytest.mark.parametrize("url", ["ftp://x.com", "javascript:alert(1)", "https://kut.nz/abc", "https://www.kut.nz/x",
                                 "https://d111.cloudfront.net/x", "http://", "https://user:pw@x.com", "not a url"])
def test_rejected_urls(url):
    with pytest.raises(UrlError):
        validate_target(url, {"kut.nz", "www.kut.nz", "d111.cloudfront.net"})


# ---------- API + redirección ----------

def create(client, body, headers=None):
    r = client.post("/api/v1/links", json=body, headers=headers or {})
    assert r.status_code == 201, r.text
    return r.json()["data"]


def test_anonymous_shorten_and_redirect(env):
    client, _ = env
    link = create(client, {"url": "https://www.iteso.mx/"})
    assert link["short_url"].startswith("https://kut.nz/")
    r = client.get(f"/{link['code']}", headers=BROWSER)
    assert r.status_code == 302 and r.headers["location"] == "https://www.iteso.mx/"
    assert "no-store" in r.headers["cache-control"]
    # insensible a mayúsculas
    assert client.get(f"/{link['code'].upper()}", headers=BROWSER).status_code == 302


def test_anonymous_cannot_use_advanced_features(env):
    client, _ = env
    r = client.post("/api/v1/links", json={"url": "https://a.com", "alias": "hola"})
    assert r.status_code == 403


def test_anonymous_rate_limit(env):
    client, _ = env
    codes_ = [client.post("/api/v1/links", json={"url": "https://a.com"}).status_code for _ in range(12)]
    assert codes_.count(201) == 10 and codes_[-1] == 429


def test_alias_unique_case_insensitive(env):
    client, _ = env
    create(client, {"url": "https://a.com", "alias": "Mi-CV"}, auth())
    r = client.post("/api/v1/links", json={"url": "https://b.com", "alias": "mi-cv"}, headers=auth("beto"))
    assert r.status_code == 422


def test_404_and_preview(env):
    client, _ = env
    assert client.get("/zzzzzzz").status_code == 404
    link = create(client, {"url": "https://example.org/destino"})
    r = client.get(f"/{link['code']}+")
    assert r.status_code == 200 and "example.org/destino" in r.text


def test_edit_reflects_immediately_despite_cache(env):
    client, _ = env
    link = create(client, {"url": "https://a.com"}, auth())
    assert client.get(f"/{link['code']}", headers=BROWSER).headers["location"] == "https://a.com"  # llena la caché
    r = client.patch(f"/api/v1/links/{link['code']}", json={"url": "https://b.com"}, headers=auth())
    assert r.status_code == 200
    assert client.get(f"/{link['code']}", headers=BROWSER).headers["location"] == "https://b.com"


def test_only_owner_can_manage(env):
    client, _ = env
    link = create(client, {"url": "https://a.com"}, auth("ana"))
    assert client.patch(f"/api/v1/links/{link['code']}", json={"url": "https://x.com"},
                        headers=auth("beto")).status_code == 404
    assert client.delete(f"/api/v1/links/{link['code']}", headers=auth("beto")).status_code == 404
    assert client.get("/api/v1/links", headers=auth("beto")).json()["count"] == 0
    assert client.get("/api/v1/links", headers=auth("ana")).json()["count"] == 1


def test_expiry_returns_410(env):
    client, _ = env
    link = create(client, {"url": "https://a.com", "expires_at": "2099-01-01T00:00:00Z"}, auth())
    from app import store
    store.update_link(link["code"], {"expires_at": int(time.time()) - 1}, None)
    from app import cache
    cache.invalidate(link["code"])
    assert client.get(f"/{link['code']}", headers=BROWSER).status_code == 410


def test_single_use_link(env):
    client, _ = env
    link = create(client, {"url": "https://a.com", "max_clicks": 1}, auth())
    assert client.get(f"/{link['code']}", headers=BROWSER).status_code == 302
    assert client.get(f"/{link['code']}", headers=BROWSER).status_code == 410


def test_preview_bots_do_not_burn_single_use_links(env):
    client, _ = env
    link = create(client, {"url": "https://a.com", "max_clicks": 1}, auth())
    r = client.get(f"/{link['code']}", headers={"User-Agent": "WhatsApp/2.23"})
    assert r.status_code == 200 and "a.com" not in r.text
    assert client.get(f"/{link['code']}", headers=BROWSER).status_code == 302


def test_click_limit_is_exact_under_concurrency(env, monkeypatch):
    client, _ = env
    link = create(client, {"url": "https://a.com", "max_clicks": 20}, auth())
    # DynamoDB evalúa la condición y escribe de forma atómica; moto no (con hilos deja pasar de más, lo
    # comprobamos sin la app). El candado hace que moto se comporte como DynamoDB real; la app sigue
    # compitiendo entre hilos, así que la prueba falla si el límite se revisara en la app y no en la BD.
    import threading

    from app import store
    real_table, lock = store.links_table, threading.Lock()

    class AtomicTable:
        def __init__(self, table):
            self._t = table

        def __getattr__(self, name):
            return getattr(self._t, name)

        def update_item(self, **kw):
            with lock:
                return self._t.update_item(**kw)

    monkeypatch.setattr(store, "links_table", lambda: AtomicTable(real_table()))
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda _: client.get(f"/{link['code']}", headers=BROWSER).status_code, range(80)))
    assert results.count(302) == 20 and results.count(410) == 60


def test_password_flow(env):
    client, _ = env
    link = create(client, {"url": "https://secreto.com", "password": "abcd1234"}, auth())
    form = client.get(f"/{link['code']}", headers=BROWSER)
    assert form.status_code == 200 and "secreto.com" not in form.text
    assert client.post(f"/{link['code']}", data={"password": "mal"}, headers=BROWSER).status_code == 401
    ok = client.post(f"/{link['code']}", data={"password": "abcd1234"}, headers=BROWSER)
    assert ok.status_code == 302 and ok.headers["location"] == "https://secreto.com"
    # vista previa no revela el destino
    assert "secreto.com" not in client.get(f"/{link['code']}+").text


def test_password_bruteforce_limited_and_failed_attempt_not_counted(env):
    client, _ = env
    link = create(client, {"url": "https://s.com", "password": "abcd1234", "max_clicks": 1}, auth())
    statuses = [client.post(f"/{link['code']}", data={"password": "x"}, headers=BROWSER).status_code
                for _ in range(6)]
    assert statuses[:5] == [401] * 5 and statuses[5] == 429
    detail = client.get(f"/api/v1/links/{link['code']}", headers=auth()).json()["data"]
    assert detail["clicks"] == 0


def test_idempotency_key(env):
    client, _ = env
    h = {**auth(), "Idempotency-Key": "abc-123"}
    a = client.post("/api/v1/links", json={"url": "https://a.com"}, headers=h).json()["data"]
    b = client.post("/api/v1/links", json={"url": "https://a.com"}, headers=h).json()["data"]
    assert a["code"] == b["code"]
    assert client.post("/api/v1/links", json={"url": "https://other.com"}, headers=h).status_code == 422


def test_qr_encodes_short_url(env):
    client, _ = env
    link = create(client, {"url": "https://a.com"})
    png = client.get(f"/api/v1/links/{link['code']}/qr.png")
    svg = client.get(f"/api/v1/links/{link['code']}/qr.svg?download=true")
    assert png.headers["content-type"] == "image/png" and png.content[:4] == b"\x89PNG"
    assert "attachment" in svg.headers["content-disposition"]


def test_clicks_are_published_and_aggregated(env):
    client, queue = env
    link = create(client, {"url": "https://a.com"}, auth())
    client.get(f"/{link['code']}", headers={**BROWSER, "Referer": "https://www.google.com/search",
                                            "CloudFront-Viewer-Country": "MX"})
    client.get(f"/{link['code']}", headers={"User-Agent": "Googlebot/2.1"})  # bot: no se cuenta
    msgs = boto3.client("sqs", region_name="us-east-1").receive_message(QueueUrl=queue, MaxNumberOfMessages=10)
    events = [json.loads(m["Body"]) for m in msgs["Messages"]]
    assert len(events) == 1 and "ip" not in events[0]
    from worker.worker import process
    process(events)
    stats = client.get(f"/api/v1/links/{link['code']}/stats", headers=auth()).json()["data"]
    assert stats["total"] == 1 and stats["country"] == {"MX": 1} and stats["referer"] == {"google.com": 1}
    assert stats["unique_visitors"] == 1 and stats["device"] == {"desktop": 1}


def test_admin_moderation(env):
    client, _ = env
    link = create(client, {"url": "https://malo.example.com/x"}, auth("ana"))
    assert client.post("/api/v1/reports", json={"code": link["code"], "reason": "phishing"}).status_code == 201
    assert len(client.get("/api/v1/admin/reports", headers=auth("root", admin=True)).json()["data"]) == 1
    assert client.post(f"/api/v1/admin/links/{link['code']}/disable", headers=auth("root", True)).status_code == 200
    assert client.get(f"/{link['code']}", headers=BROWSER).status_code == 410
    # el dueño no puede reactivar lo que desactivó un admin
    assert client.patch(f"/api/v1/links/{link['code']}", json={"disabled": False},
                        headers=auth("ana")).status_code == 403
    # bloquear dominio impide crear nuevos enlaces hacia él
    client.post("/api/v1/admin/blocked-domains", json={"domain": "example.com"}, headers=auth("root", True))
    assert client.post("/api/v1/links", json={"url": "https://sub.example.com"}).status_code == 422
    # bloquear cuenta
    client.post("/api/v1/admin/blocked-users", json={"user_id": "dev-ana"}, headers=auth("root", True))
    assert client.get("/api/v1/links", headers=auth("ana")).status_code == 403
    assert client.get("/api/v1/admin/reports", headers=auth("ana")).status_code == 403


def test_redis_down_opens_circuit_and_redirect_still_works(env, monkeypatch):
    client, _ = env
    link = create(client, {"url": "https://a.com"})
    from app import cache
    calls = []

    class Down:
        def __getattr__(self, name):
            def fail(*a, **k):
                calls.append(name)
                raise cache.redis.ConnectionError("failover")
            return fail

    monkeypatch.setattr(cache, "client", lambda: Down())
    monkeypatch.setattr(cache, "_open_until", 0.0)
    assert client.get(f"/{link['code']}", headers=BROWSER).status_code == 302  # fail-open contra DynamoDB
    tried = len(calls)
    assert tried >= 1
    assert client.get(f"/{link['code']}", headers=BROWSER).status_code == 302
    assert len(calls) == tried  # circuito abierto: ya no se intenta Redis
    monkeypatch.setattr(cache, "_open_until", 0.0)  # pasa el tiempo de espera: se vuelve a intentar
    client.get(f"/{link['code']}", headers=BROWSER)
    assert len(calls) > tried
