"""APM con Sentry para kut.nz (Infraestructura para el Desarrollo Continuo).

Envuelve `app.main:app` sin modificar el código de la aplicación: uvicorn arranca `apm:app`.
- Sin `SENTRY_DSN` (pruebas, local) Sentry queda apagado y la app se comporta igual que antes.
- El ambiente (dev / prod) se deduce de `KUT_PUBLIC_HOST`, que ya define cada stack de AWS.
- `KUT_APM_DEMO=1` (solo en Dev) agrega dos rutas para generar un error y un mensaje a propósito.
"""
import logging
import os

import sentry_sdk
from sentry_sdk.integrations.logging import LoggingIntegration

RELEASE = os.getenv("KUT_RELEASE", "local")
HOST = os.getenv("KUT_PUBLIC_HOST", "localhost")
ENVIRONMENT = {"kut.nz": "prod", "dev.kut.nz": "dev"}.get(HOST, "local")
DEMO = os.getenv("KUT_APM_DEMO") == "1"

log = logging.getLogger("kutnz.apm")


def init_sentry() -> bool:
    dsn = os.getenv("SENTRY_DSN", "")
    if not dsn:
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=ENVIRONMENT,
        release=f"kutnz-backend@{RELEASE}",
        traces_sample_rate=0.2 if ENVIRONMENT == "prod" else 1.0,
        send_default_pii=False,  # kut.nz no guarda IPs: tampoco se mandan a Sentry
        enable_logs=True,
        integrations=[LoggingIntegration(level=logging.INFO, event_level=logging.ERROR,
                                         sentry_logs_level=logging.INFO)],
    )
    return True


# Sentry debe inicializarse antes de crear la app para instrumentar FastAPI/Starlette.
SENTRY_ON = init_sentry()

from app.main import app  # noqa: E402


@app.middleware("http")
async def version_header(request, call_next):
    response = await call_next(request)
    response.headers["X-Kut-Version"] = RELEASE
    return response


if DEMO:
    @app.get("/_apm/error", include_in_schema=False)
    def apm_error():
        raise RuntimeError(f"Error de demostración de APM en {ENVIRONMENT} ({RELEASE})")

    @app.get("/_apm/message", include_in_schema=False)
    def apm_message():
        log.warning("Mensaje de demostración de APM en %s (%s)", ENVIRONMENT, RELEASE)
        sentry_sdk.capture_message(f"Mensaje de demostración de APM en {ENVIRONMENT}", level="info")
        return {"data": {"sent": SENTRY_ON, "environment": ENVIRONMENT, "release": RELEASE}}
