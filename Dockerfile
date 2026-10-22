# ──────────────────────────────────────────────
# Base stage: shared between release and debug
# ──────────────────────────────────────────────
FROM python:3.14-slim AS base

# uv installs the locked dependencies (uv.lock) into /app/.venv, using the
# image's system Python (never a downloaded one). The venv comes first on PATH,
# so plain `python` / `gunicorn` (entrypoint.sh, compose commands) use it.
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/app/data/db.sqlite3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc curl \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first (cached layer), application code afterwards.
# --frozen: install exactly what uv.lock pins; --no-dev: runtime only (the
# dev/debug/docs groups stay out of the image).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .

RUN mkdir -p /app/media /app/data

COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENTRYPOINT ["/app/entrypoint.sh"]

# ──────────────────────────────────────────────
# Release stage: production-ready image
# ──────────────────────────────────────────────
FROM base AS release

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# Production-ready defaults — override in docker-compose.yml or .env as needed
ENV DEBUG=0 \
    ALLOWED_HOSTS=localhost,127.0.0.1 \
    ORCASLICER_API_URL=http://orcaslicer:3000 \
    SPOOLMAN_URL=http://spoolman:8000

LABEL org.opencontainers.image.title="LayerNexus" \
      org.opencontainers.image.description="The control center for your 3D print workflow" \
      org.opencontainers.image.source="https://github.com/peterus/LayerNexus" \
      org.opencontainers.image.licenses="GPL-3.0" \
      org.opencontainers.image.version="${APP_VERSION}"

RUN DJANGO_SECRET_KEY=build-placeholder python manage.py collectstatic --noinput

# Give appuser a real, writable home directory. `adduser --system` otherwise
# leaves HOME=/nonexistent, so libraries that write caches/config to $HOME
# (e.g. fontconfig, matplotlib) fail with
# "[Errno 13] Permission denied: '/nonexistent'" at runtime.
RUN addgroup --system appgroup \
    && adduser --system --ingroup appgroup --home /home/appuser appuser \
    && mkdir -p /home/appuser \
    && chown -R appuser:appgroup /app/media /app/data /app/staticfiles /home/appuser

ENV HOME=/home/appuser

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health/ || exit 1

CMD ["gunicorn", "layernexus.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]

# ──────────────────────────────────────────────
# Debug stage: development with debugpy + runserver
# ──────────────────────────────────────────────
FROM base AS debug

RUN uv sync --frozen --no-dev --group debug --no-install-project

ENV DEBUG=1

EXPOSE 8000 5678

CMD ["python", "-m", "debugpy", "--listen", "0.0.0.0:5678", \
     "manage.py", "runserver", "0.0.0.0:8000"]
