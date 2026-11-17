"""kut.nz — API REST + redirección."""
import logging
import time
from pathlib import Path
from urllib.parse import urlsplit

import segno
from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import cache, clicks, codes, links, security, store
from .auth import User, optional_user, require_admin, require_user
from .config import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("kutnz")

BASE = Path(__file__).parent
settings = get_settings()
API_DESCRIPTION = """
Todo lo que hace la web de [kut.nz](/) se puede hacer con esta API REST (respuestas JSON).

* **Sin cuenta:** crear enlaces cortos y obtener su QR (con límite por IP).
* **Con cuenta:** alias, edición, caducidad, límite de clics, contraseña, métricas y moderación.
  Presiona **Authorize** y pega tu *access token* de sesión.
* `POST /api/v1/links` acepta la cabecera `Idempotency-Key` para reintentar sin crear duplicados.
"""
app = FastAPI(title="kut.nz API", version="1.0.0", description=API_DESCRIPTION, docs_url="/api/docs",
              openapi_url="/api/openapi.json", redoc_url=None)
templates = Jinja2Templates(directory=BASE / "templates")
templates.env.globals["static"] = settings.static_base_url
if settings.serve_static_locally:
    app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")

NO_STORE = {"Cache-Control": "private, no-store, max-age=0"}


# ---------------- middleware ----------------

@app.middleware("http")
async def security_headers(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    if request.url.path == "/api/docs":
        # Swagger UI (documentación interactiva) carga su JS/CSS desde jsdelivr y usa un script en línea.
        # Política más permisiva SOLO para esta página, que no muestra datos de usuarios.
        csp = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
               "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data: https://fastapi.tiangolo.com; "
               f"connect-src 'self'; frame-ancestors {settings.frame_ancestors}; base-uri 'none'")
    else:
        # El login propio habla directo con la API de Cognito desde el navegador.
        cognito = f"https://cognito-idp.{settings.aws_region}.amazonaws.com" if settings.auth_mode == "cognito" else ""
        csp = ("default-src 'self'; img-src 'self' data:; style-src 'self' https://fonts.googleapis.com; "
               "style-src-attr 'unsafe-inline'; "
               f"font-src https://fonts.gstatic.com; connect-src 'self' {cognito}; "
               f"frame-ancestors {settings.frame_ancestors}; base-uri 'none'; form-action 'self'")
    response.headers.setdefault("Content-Security-Policy", csp)
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - started) * 1000:.1f}"
    return response


def client_ip(request: Request) -> str:
    # CloudFront entrega la IP real del visitante en esta cabecera (configurada en la origin request policy).
    viewer = request.headers.get("cloudfront-viewer-address")
    if viewer:
        return viewer.rsplit(":", 1)[0].strip("[]")
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


def envelope(data=None, status: int = 200, **extra) -> JSONResponse:
    return JSONResponse({"data": data, **extra}, status_code=status)


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": {"status": exc.status_code, "message": exc.detail}}, status_code=exc.status_code,
                            headers=getattr(exc, "headers", None))
    return status_page(request, exc.status_code, str(exc.detail))


# ---------------- salud ----------------

@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True}  # el ALB solo necesita saber que el proceso responde


@app.get("/readyz", include_in_schema=False)
def readyz():
    ok_cache = cache.ping()
    try:
        store.get_link("__readyz__")
        ok_db = True
    except Exception:  # noqa: BLE001
        ok_db = False
    return JSONResponse({"dynamodb": ok_db, "redis": ok_cache}, status_code=200 if ok_db else 503)


# ---------------- API: enlaces ----------------

class LinkIn(BaseModel):
    url: str = Field(..., examples=["https://www.iteso.mx/"])
    alias: str | None = None
    title: str | None = None
    expires_at: str | None = Field(None, description="ISO 8601, p. ej. 2026-12-31T23:59:00-06:00")
    max_clicks: int | None = None
    password: str | None = None


class LinkPatch(BaseModel):
    url: str | None = None
    title: str | None = None
    expires_at: str | None = None
    max_clicks: int | None = None
    password: str | None = None
    disabled: bool | None = None


def _translate(fn):
    try:
        return fn()
    except links.InvalidInput as exc:
        raise HTTPException(422, str(exc)) from exc
    except links.Forbidden as exc:
        raise HTTPException(403, str(exc)) from exc
    except store.NotFound as exc:
        raise HTTPException(404, "Enlace no encontrado.") from exc


@app.post("/api/v1/links", status_code=201, tags=["enlaces"])
def create_link(body: LinkIn, request: Request, user: User | None = Depends(optional_user)):
    idem = request.headers.get("idempotency-key")
    idem_key = f"{user.id if user else client_ip(request)}:{idem}" if idem else None
    if idem_key and (prev := cache.idempotency_get(idem_key)):
        if prev["request"] != body.model_dump():
            raise HTTPException(422, "Idempotency-Key reutilizada con un cuerpo distinto.")
        return envelope(prev["response"], status=201, replayed=True)

    if user:
        allowed = cache.hit(f"create:user:{user.id}", settings.user_creates_per_hour, 3600)
    else:
        allowed = cache.hit(f"create:ip:{client_ip(request)}", settings.anon_creates_per_hour, 3600)
    if not allowed:
        raise HTTPException(429, "Límite de creaciones alcanzado. Intenta más tarde.")

    link = _translate(lambda: links.create(body.model_dump(), user.id if user else None))
    view = links.public_view(link)
    if idem_key:
        cache.idempotency_put(idem_key, {"request": body.model_dump(), "response": view})
    return envelope(view, status=201)


@app.get("/api/v1/links", tags=["enlaces"])
def list_links(q: str = "", sort: str = "-created_at", user: User = Depends(require_user)):
    items = links.list_for(user, q=q, sort=sort)
    return envelope(items, count=len(items))


@app.get("/api/v1/links/{code}", tags=["enlaces"])
def get_link(code: str, user: User = Depends(require_user)):
    return envelope(links.public_view(_translate(lambda: links.get_owned(code, user))))


@app.patch("/api/v1/links/{code}", tags=["enlaces"])
def patch_link(code: str, body: LinkPatch, user: User = Depends(require_user)):
    data = body.model_dump(exclude_unset=True)
    return envelope(links.public_view(_translate(lambda: links.update(code, data, user))))


@app.delete("/api/v1/links/{code}", status_code=204, tags=["enlaces"])
def delete_link(code: str, user: User = Depends(require_user)):
    _translate(lambda: links.delete(code, user))
    return Response(status_code=204)


@app.get("/api/v1/links/{code}/stats", tags=["métricas"])
def link_stats(code: str, user: User = Depends(require_user)):
    link = _translate(lambda: links.get_owned(code, user))
    out = {"total": link.get("clicks", 0), "by_day": {}, "unique_by_day": {}, "country": {}, "device": {},
           "browser": {}, "os": {}, "referer": {}}
    for item in store.get_stats(link["code"]):
        kind, _, value = item["dim"].partition("#")
        if kind == "uv":
            out["unique_by_day"][value] = len(item.get("visitors", []))
        elif kind == "day":
            out["by_day"][value] = item["n"]
        elif kind in out:
            out[kind][value] = item["n"]
    out["unique_visitors"] = sum(out["unique_by_day"].values())
    out["by_day"] = dict(sorted(out["by_day"].items()))
    return envelope(out)


def _qr(code: str):
    link = store.get_link(codes.normalize(code))
    if not link:
        raise HTTPException(404, "Enlace no encontrado.")
    # El QR codifica el enlace corto, no el destino: si el destino se edita, el QR sigue sirviendo.
    return segno.make(links.short_url(link["code"]), error="m"), link["code"]


@app.get("/api/v1/links/{code}/qr.png", tags=["enlaces"])
def qr_png(code: str, download: bool = False):
    import io
    qr, c = _qr(code)
    buf = io.BytesIO()
    qr.save(buf, kind="png", scale=12, border=2, dark="#273462", light="#ffffff")
    headers = {"Cache-Control": "public, max-age=86400"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="kutnz-{c}.png"'
    return Response(buf.getvalue(), media_type="image/png", headers=headers)


@app.get("/api/v1/links/{code}/qr.svg", tags=["enlaces"])
def qr_svg(code: str, download: bool = False):
    import io
    qr, c = _qr(code)
    buf = io.BytesIO()
    qr.save(buf, kind="svg", scale=10, border=2, dark="#273462", light="#ffffff", xmldecl=False)
    headers = {"Cache-Control": "public, max-age=86400"}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="kutnz-{c}.svg"'
    return Response(buf.getvalue(), media_type="image/svg+xml", headers=headers)


# ---------------- reportes y administración ----------------

class ReportIn(BaseModel):
    code: str
    reason: str = Field("", max_length=500)


@app.post("/api/v1/reports", status_code=201, tags=["moderación"])
def report(body: ReportIn, request: Request):
    if not cache.hit(f"report:{client_ip(request)}", 20, 3600):
        raise HTTPException(429, "Demasiados reportes. Intenta más tarde.")
    code = codes.normalize(body.code).rstrip("+")
    if not store.get_link(code):
        raise HTTPException(404, "Enlace no encontrado.")
    store.moderation_put("report", f"{int(time.time())}#{code}", {"code": code, "reason": body.reason.strip()})
    return envelope({"ok": True}, status=201)


@app.get("/api/v1/admin/reports", tags=["moderación"])
def admin_reports(_: User = Depends(require_admin)):
    return envelope(store.moderation_list("report"))


class DomainIn(BaseModel):
    domain: str


class UserIn(BaseModel):
    user_id: str


@app.post("/api/v1/admin/links/{code}/disable", tags=["moderación"])
def admin_disable(code: str, admin: User = Depends(require_admin)):
    return envelope(links.public_view(_translate(lambda: links.update(code, {"disabled": True}, admin))))


@app.post("/api/v1/admin/links/{code}/enable", tags=["moderación"])
def admin_enable(code: str, admin: User = Depends(require_admin)):
    return envelope(links.public_view(_translate(lambda: links.update(code, {"disabled": False}, admin))))


@app.post("/api/v1/admin/blocked-domains", status_code=201, tags=["moderación"])
def admin_block_domain(body: DomainIn, _: User = Depends(require_admin)):
    domain = body.domain.strip().lower().removeprefix("www.")
    if "." not in domain:
        raise HTTPException(422, "Dominio inválido.")
    store.moderation_put("domain", domain)
    _blocked_domains.clear()
    return envelope({"domain": domain}, status=201)


@app.delete("/api/v1/admin/blocked-domains/{domain}", status_code=204, tags=["moderación"])
def admin_unblock_domain(domain: str, _: User = Depends(require_admin)):
    store.moderation_delete("domain", domain.lower())
    _blocked_domains.clear()
    return Response(status_code=204)


@app.post("/api/v1/admin/blocked-users", status_code=201, tags=["moderación"])
def admin_block_user(body: UserIn, _: User = Depends(require_admin)):
    store.moderation_put("user", body.user_id)
    return envelope({"user_id": body.user_id}, status=201)


@app.delete("/api/v1/admin/blocked-users/{user_id}", status_code=204, tags=["moderación"])
def admin_unblock_user(user_id: str, _: User = Depends(require_admin)):
    store.moderation_delete("user", user_id)
    return Response(status_code=204)


@app.get("/api/v1/config", tags=["sistema"])
def frontend_config():
    return envelope({
        "base_url": settings.base_url,
        "host": settings.public_host,
        "auth_mode": settings.auth_mode,
        "cognito_domain": settings.cognito_domain,
        "cognito_client_id": settings.cognito_client_id,
        "cognito_region": settings.aws_region,
    })


@app.get("/api/v1/me", tags=["sistema"])
def me(user: User = Depends(require_user)):
    return envelope({"id": user.id, "email": user.email, "is_admin": user.is_admin})


# ---------------- páginas HTML ----------------

SPA_ROUTES = ("/", "/panel", "/admin", "/auth/callback")


def spa():
    return FileResponse(BASE / "static" / "index.html", headers={"Cache-Control": "no-cache"})


for _route in SPA_ROUTES:
    app.add_api_route(_route, spa, methods=["GET"], include_in_schema=False)


STATUS_COPY = {
    404: ("Este enlace no existe", "Revisa que esté bien escrito. Los enlaces de kut.nz no distinguen mayúsculas."),
    410: ("Este enlace ya no está disponible", ""),
    429: ("Demasiados intentos", "Espera unos minutos antes de volver a intentarlo."),
}
REASONS = {
    "caducado": "Llegó a su fecha de caducidad.",
    "agotado": "Alcanzó el número máximo de visitas que permitió su dueño.",
    "desactivado": "Su dueño o un administrador lo desactivó.",
}


def status_page(request: Request, status: int, detail: str = "", code: str | None = None) -> HTMLResponse:
    title, body = STATUS_COPY.get(status, ("Algo salió mal", detail))
    return templates.TemplateResponse(
        request, "status.html",
        {"status": status, "title": title, "body": REASONS.get(detail, body or detail), "code": code,
         "base": settings.base_url},
        status_code=status, headers=NO_STORE,
    )


# Dominios bloqueados: caché en memoria de 60 s para no consultar DynamoDB en cada visita.
_blocked_domains: dict = {}


def target_blocked(target: str) -> bool:
    if _blocked_domains.get("exp", 0) < time.time():
        _blocked_domains.update(set={i["value"] for i in store.moderation_list("domain", limit=1000)},
                                exp=time.time() + 60)
    host = (urlsplit(target).hostname or "").lower()
    parts = host.split(".")
    return any(".".join(parts[i:]) in _blocked_domains["set"] for i in range(len(parts) - 1))


def resolve(code: str) -> dict | None:
    view = cache.get_link(code)
    if view == cache.NOT_FOUND:
        return None
    if view is None:
        link = store.get_link(code)
        if not link:
            cache.put_not_found(code)
            return None
        view = links.cache_view(link)
        cache.put_link(code, view)
    return view


def unavailable_reason(view: dict, t: int) -> str | None:
    if view["disabled"]:
        return "desactivado"
    if view.get("expires_at") and view["expires_at"] <= t:
        return "caducado"
    if view.get("max_clicks") and view.get("clicks", 0) >= view["max_clicks"]:
        return "agotado"
    if target_blocked(view["target"]):
        return "desactivado"
    return None


def complete_visit(request: Request, view: dict, background: BackgroundTasks):
    ua = request.headers.get("user-agent", "")
    bot = clicks.is_bot(ua)
    counted = False
    if view.get("max_clicks"):
        if bot:
            # Los generadores de vista previa (WhatsApp, Slack…) gastarían un enlace de un solo uso:
            # reciben una página neutral, sin destino y sin consumir el clic.
            return templates.TemplateResponse(
                request, "status.html",
                {"status": 200, "title": "Enlace de uso limitado", "body": "Ábrelo en un navegador para continuar.",
                 "code": view["code"], "base": settings.base_url}, headers=NO_STORE)
        # El clic se reserva de forma atómica en DynamoDB antes de redirigir.
        if not store.consume_limited_click(view["code"], links.now()):
            cache.invalidate(view["code"])
            return status_page(request, 410, "agotado", view["code"])
        cache.invalidate(view["code"])
        counted = True
    if not bot:
        event = clicks.build_event(view["code"], client_ip(request), ua, request.headers.get("referer", ""),
                                   request.headers.get("cloudfront-viewer-country", ""), counted)
        background.add_task(clicks.publish, event)  # se envía después de responder: no retrasa la redirección
    return RedirectResponse(view["target"], status_code=302, headers=NO_STORE)


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return Response("User-agent: *\nDisallow: /\nAllow: /$\n", media_type="text/plain")


@app.get("/{raw_code}", include_in_schema=False)
def visit(raw_code: str, request: Request, background: BackgroundTasks):
    if raw_code.endswith("+"):
        return preview(request, raw_code[:-1])
    code = codes.normalize(raw_code)
    view = resolve(code)
    if view is None:
        return status_page(request, 404, code=code)
    if reason := unavailable_reason(view, links.now()):
        return status_page(request, 410, reason, code)
    if view["has_password"]:
        return templates.TemplateResponse(request, "password.html", {"code": code, "error": None,
                                                                      "base": settings.base_url}, headers=NO_STORE)
    return complete_visit(request, view, background)


@app.post("/{raw_code}", include_in_schema=False)
def visit_with_password(raw_code: str, request: Request, background: BackgroundTasks, password: str = Form("")):
    code = codes.normalize(raw_code)
    view = resolve(code)
    if view is None:
        return status_page(request, 404, code=code)
    if reason := unavailable_reason(view, links.now()):
        return status_page(request, 410, reason, code)
    if not view["has_password"]:
        return complete_visit(request, view, background)
    if not cache.hit(f"pwd:{code}:{client_ip(request)}", settings.password_attempts_per_15min, 900):
        return status_page(request, 429, code=code)
    link = store.get_link(code)
    if not link or not security.verify_password(link.get("password_hash", ""), password):
        # Un intento fallido no cuenta como clic.
        return templates.TemplateResponse(request, "password.html",
                                          {"code": code, "error": "Contraseña incorrecta.", "base": settings.base_url},
                                          status_code=401, headers=NO_STORE)
    return complete_visit(request, view, background)


def preview(request: Request, raw_code: str):
    code = codes.normalize(raw_code)
    link = store.get_link(code)
    if not link:
        return status_page(request, 404, code=code)
    protected = bool(link.get("password_hash"))
    return templates.TemplateResponse(
        request, "preview.html",
        {"code": code, "short_url": links.short_url(code),
         # Un enlace con contraseña no revela su destino en la vista previa.
         "target": None if protected else link["target"],
         "host": None if protected else urlsplit(link["target"]).hostname,
         "status": links.status_of(link), "has_password": protected, "base": settings.base_url},
        headers=NO_STORE,
    )
