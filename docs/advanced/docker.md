# Docker Details

This page covers the finer details of running LayerNexus with Docker — image tags, health checks, the full-stack compose setup, and more. For basic setup, see the [Quick Start](../quick-start.md).

---

## Image Registry

LayerNexus images are hosted on the GitHub Container Registry:

```
ghcr.io/peterus/layernexus
```

### Available Tags

| Tag | What It Is |
|---|---|
| `latest` | The most recent stable release — this is what most people should use |
| `<version>` | A specific version (e.g., `0.1.0`) — use this to pin a known-good version |
| `sha-<commit>` | A build from a specific commit — for advanced users or testing |

To use a specific version:

```yaml
image: ghcr.io/peterus/layernexus:0.1.0
```

---

## Health Check

The LayerNexus container includes a built-in health check that verifies the app is responding:

```
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3
  CMD curl -f http://localhost:8000/health/ || exit 1
```

Check the health status with:

```bash
docker inspect --format='{{.State.Health.Status}}' layernexus
```

This is useful for orchestrators like Docker Swarm or Kubernetes, or for monitoring tools.

---

## Automatic Setup on Start

Every time the container starts, it automatically:

1. **Runs database migrations** — so the database schema is always up to date after an upgrade
2. **Collects static files** — so CSS, JavaScript, and images are ready to serve

You never need to run these commands manually.

---

## Full Stack with Spoolman

To run LayerNexus with both OrcaSlicer and Spoolman for filament tracking:

```yaml
services:
  web:
    image: ghcr.io/peterus/layernexus:latest
    ports:
      - "8000:8000"
    volumes:
      - layernexus_data:/app/data
      - layernexus_media:/app/media
    environment:
      - DJANGO_SECRET_KEY=your-long-random-secret-key
    restart: unless-stopped
    depends_on:
      - orcaslicer
      - spoolman

  orcaslicer:
    image: ghcr.io/afkfelix/orca-slicer-api:latest-orca2.3.1
    restart: unless-stopped

  spoolman:
    image: ghcr.io/donkie/spoolman:latest
    ports:
      - "7912:8000"
    volumes:
      - spoolman_data:/home/app/.local/share/spoolman
    restart: unless-stopped

volumes:
  layernexus_data:
  layernexus_media:
  spoolman_data:
```

!!! info "Sensible Defaults"
    The Docker image already sets `ORCASLICER_API_URL=http://orcaslicer:3000` and `SPOOLMAN_URL=http://spoolman:8000` by default. As long as you name your services `orcaslicer` and `spoolman`, no extra configuration is needed.

!!! note "Moonraker"
    Moonraker runs on your 3D printer itself, not as a Docker container alongside LayerNexus. You configure the Moonraker URL (e.g., `http://192.168.1.100:7125`) in the LayerNexus printer profile settings. See [Klipper / Moonraker](../integrations/moonraker.md).

---

## Running Without Docker Compose

You can also run LayerNexus with a single `docker run` command, though you won't get OrcaSlicer or Spoolman integration this way:

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
    Without the OrcaSlicer and Spoolman containers, slicing and filament tracking will not work. Use Docker Compose for the full experience.

---

## Debug Mode

For troubleshooting, you can enable debug mode by setting `DEBUG=1` in your environment. This shows more detailed error pages and enables verbose logging.

!!! danger "Never Use Debug Mode in Production"
    Debug mode exposes detailed error information that could be a security risk. Only enable it temporarily for troubleshooting.

---

## Next Steps

- [HTTPS & Reverse Proxy](reverse-proxy.md)
- [Backup & Restore](backup.md)
- [Configuration](../configuration.md)
