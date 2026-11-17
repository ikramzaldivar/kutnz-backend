FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY worker ./worker
COPY apm.py .
# APM (Infraestructura para el Desarrollo Continuo): el pipeline inyecta el DSN de Sentry y el commit.
ARG SENTRY_DSN=""
ARG KUT_RELEASE=local
ENV SENTRY_DSN=$SENTRY_DSN KUT_RELEASE=$KUT_RELEASE
RUN useradd --system --uid 10001 kut && chown -R kut /srv
USER kut
EXPOSE 8000
# Por defecto corre la API (envuelta por apm.py); el worker usa la misma imagen con: python -m worker.worker
CMD ["uvicorn", "apm:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--proxy-headers", "--forwarded-allow-ips", "*"]
