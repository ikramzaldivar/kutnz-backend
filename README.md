# kutnz-backend

API y redirección de **kut.nz**, un acortador de URLs en AWS: FastAPI, DynamoDB, Valkey, SQS y Cognito. Es el backend del proyecto final de *Infraestructura para el Desarrollo Continuo* (ITESO, 2026).

| Ambiente | URL | Despliegue |
| --- | --- | --- |
| Dev | https://dev.kut.nz | Automático con cada merge a `main` |
| Prod | https://kut.nz | Después de Dev, con aprobación del environment `prod` |

La documentación completa está en la [Wiki](../../wiki): arquitectura, componentes, diagramas, pipelines y plan de trabajo.

## Repositorios del proyecto

- **kutnz-backend** (este): API FastAPI, worker de clics, pruebas y Dockerfile.
- [kutnz-frontend](https://github.com/ikramzaldivar/kutnz-frontend): SPA (`/static/*` en S3 + CloudFront).
- [kutnz-infra](https://github.com/ikramzaldivar/kutnz-infra): Terraform del ambiente Dev y de los roles de CI/CD.
- [kutnz](https://github.com/ikramzaldivar/kutnz): proyecto original de Diseño de Sistemas Escalables. Este repo parte de una copia; ese repo no se modifica.

## Estructura del código

| Ruta | Contenido |
| --- | --- |
| `app/main.py` | Rutas de FastAPI: API, redirección y páginas de estado |
| `app/links.py` | Reglas de negocio de los enlaces, independientes de HTTP |
| `app/store.py` | Acceso a DynamoDB |
| `app/cache.py` | Caché de Redis, rate limiting e idempotencia |
| `app/clicks.py` | Publicación de eventos de clic en SQS |
| `app/auth.py` | Autenticación con tokens de Cognito (y modo `dev` en local) |
| `app/security.py` | Contraseñas de enlaces (argon2) y revisión de destinos con Safe Browsing |
| `app/codes.py`, `app/urls.py` | Validación de alias y de URLs de destino |
| `worker/worker.py` | Consumidor de SQS que agrega las estadísticas |
| `apm.py` | Envoltura de Sentry |

## Desarrollo local (sin AWS)

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements-dev.txt
.venv/Scripts/python -m pytest -q --cov=app --cov=worker --cov=apm   # pruebas + cobertura
.venv/Scripts/python scripts/dev_server.py                             # http://localhost:8000
```

## Pipeline (`.github/workflows/pipeline.yml`)

1. **Build:** dependencias e imagen Docker (el DSN de Sentry y el commit se inyectan como `build-arg`).
2. **UAT:** pytest con reporte JUnit y cobertura; falla si la cobertura baja de 80 %.
3. **Publicar:** la imagen va a Docker Hub como `ikramzaldivar/kutnz-api:sha-<commit>`.
4. **Deploy Dev:** se copia la imagen al ECR de Dev, se hace un instance refresh y un smoke test con `X-Kut-Version`.
5. **Aprobación:** environment `prod`.
6. **Deploy Prod:** la misma imagen va a Prod y se crean el tag `v1.0.<n>` y `latest`.

La conexión con AWS es por OIDC: cada environment tiene su propio `AWS_ROLE_ARN` y no hay llaves guardadas.

## APM

`apm.py` inicializa Sentry y envuelve `app.main:app` sin modificar el código de la aplicación. El ambiente se deduce de `KUT_PUBLIC_HOST`. En Dev, `KUT_APM_DEMO=1` habilita `/_apm/error` y `/_apm/message`.

## Ramas

GitHub Flow: `feature/<issue>-<descripcion>` → PR a `main` (CI en verde y una aprobación) → Dev → aprobación → Prod. Cada PR cierra su tarea con `Closes #n`.
