"""Pruebas del módulo de APM (apm.py): no debe cambiar el comportamiento de la app."""
import importlib
import sys

from fastapi.testclient import TestClient


def load_apm(monkeypatch, **env):
    for key in ("SENTRY_DSN", "KUT_APM_DEMO", "KUT_RELEASE", "KUT_PUBLIC_HOST"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    for mod in [m for m in sys.modules if m == "apm" or m == "app.main"]:
        del sys.modules[mod]
    return importlib.import_module("apm")


def test_without_dsn_sentry_is_off_and_app_works(env, monkeypatch):
    apm = load_apm(monkeypatch, KUT_RELEASE="abc1234")
    assert apm.SENTRY_ON is False
    r = TestClient(apm.app).get("/healthz")
    assert r.status_code == 200
    assert r.headers["X-Kut-Version"] == "abc1234"


def test_environment_from_public_host(env, monkeypatch):
    assert load_apm(monkeypatch, KUT_PUBLIC_HOST="kut.nz").ENVIRONMENT == "prod"
    assert load_apm(monkeypatch, KUT_PUBLIC_HOST="dev.kut.nz").ENVIRONMENT == "dev"
    assert load_apm(monkeypatch).ENVIRONMENT == "local"


def test_demo_routes_only_when_enabled(env, monkeypatch):
    apm = load_apm(monkeypatch)
    assert TestClient(apm.app).get("/_apm/message").status_code == 404

    apm = load_apm(monkeypatch, KUT_APM_DEMO="1")
    client = TestClient(apm.app, raise_server_exceptions=False)
    assert client.get("/_apm/message").json()["data"]["sent"] is False
    assert client.get("/_apm/error").status_code == 500
