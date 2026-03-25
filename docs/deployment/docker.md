# Docker Image

LayerNexus is distributed as a Docker image, available from the GitHub Container Registry.

## Image Registry

```
ghcr.io/peterus/layernexus
```

## Available Tags

| Tag | Description |
|---|---|
| `latest` | Latest release from the `main` branch |
| `sha-<commit>` | Build from a specific commit SHA |
| `<version>` | Semantic version tags (e.g., `0.1.0`) |

---

## Minimal Docker Run

To run LayerNexus with a single command:

```bash
docker run -d \
  --name layernexus \
  -p 8000:8000 \
  -v layernexus_data:/app/data \
  -v layernexus_media:/app/media \
  -e DJANGO_SECRET_KEY="your-secret-key-here" \
  -e DEBUG=0 \
  -e ALLOWED_HOSTS="localhost" \
  ghcr.io/peterus/layernexus:latest
```

!!! warning
    This runs LayerNexus without the OrcaSlicer API container. Slicing features will not be available. For the full stack, use [Docker Compose](docker-compose.md).

---

## Volume Mounts

The container requires two persistent volumes:

| Container Path | Purpose | Recommended Mount |
|---|---|---|
| `/app/data/` | SQLite database (`db.sqlite3`) | Named volume or bind mount |
| `/app/media/` | User uploads (STL, G-code, images, documents) | Named volume or bind mount |

!!! danger "Data Loss"
    Without persistent volumes, all data is lost when the container is recreated.

---

## Multi-Stage Build

The Dockerfile uses a multi-stage build with two targets:

### Release Target (Production)

```bash
docker build --target release -t layernexus .
```

- Runs `collectstatic` during build
- Includes a health check at `/health/`
- Serves with Gunicorn (3 workers)
- Exposes port `8000`

### Debug Target (Development)

```bash
docker build --target debug -t layernexus:debug .
```

- Installs `debugpy` and `django-debug-toolbar`
- Runs Django development server
- Enables remote debugging on port `5678`
- Sets `DEBUG=1` by default
- Exposes ports `8000` (web) and `5678` (debugger)

---

## Health Check

The release image includes a built-in health check:

```
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3
  CMD curl -f http://localhost:8000/health/ || exit 1
```

You can verify the container health with:

```bash
docker inspect --format='{{.State.Health.Status}}' layernexus
```

---

## Entrypoint

The container entrypoint script (`entrypoint.sh`) runs automatically on startup:

1. **Database migrations** — applies any pending Django migrations
2. **Static file collection** — collects static files for WhiteNoise

This means you never need to manually run migrations after upgrading the image.

---

## Next Steps

- [Docker Compose examples for production](docker-compose.md)
- [Set up a reverse proxy](reverse-proxy.md)
- [Backup & restore procedures](backup.md)
