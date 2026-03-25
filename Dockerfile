# ──────────────────────────────────────────────
# Base stage: shared between release and debug
# ──────────────────────────────────────────────
FROM python:3.14-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATABASE_PATH=/app/data/db.sqlite3

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

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
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${APP_VERSION}"

RUN DJANGO_SECRET_KEY=build-placeholder python manage.py collectstatic --noinput

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health/ || exit 1

CMD ["gunicorn", "layernexus.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3"]

# ──────────────────────────────────────────────
# Debug stage: development with debugpy + runserver
# ──────────────────────────────────────────────
FROM base AS debug

RUN pip install --no-cache-dir debugpy django-debug-toolbar

ENV DEBUG=1

EXPOSE 8000 5678

CMD ["python", "-m", "debugpy", "--listen", "0.0.0.0:5678", \
     "manage.py", "runserver", "0.0.0.0:8000"]
