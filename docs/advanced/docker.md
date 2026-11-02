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
| `latest` | The most recent release — this is what most people should use |
| `<version>` | A specific release (e.g., `0.2.0`) — use this to pin a known-good version |
| `<major>.<minor>` | The newest patch release of a minor line (e.g., `0.2`) |
| `main` | The newest build of `main`, released or not |
| `sha-<commit>` | A build from a specific commit (first 7 characters) — for advanced users or testing |

To use a specific version:

```yaml
image: ghcr.io/peterus/layernexus:0.2.0
```

### How Releases Are Made

Releases are automatic. After CI is green on `main`, the release workflow reads the
pull request titles (Conventional Commits) merged since the last `vX.Y.Z` tag:

| PR title | Effect |
|---|---|
| `feat: …` / `feat(scope): …` | new minor release (`0.2.0` → `0.3.0`) |
| `fix: …` | new patch release (`0.2.0` → `0.2.1`) |
| `feat!: …` or a `BREAKING CHANGE:` footer | new major release (`0.2.0` → `1.0.0`) |
| anything else (`chore`, `docs`, `ci`, `build`, `refactor`, `test`, …) | no release |

A release gets a git tag, a [GitHub release](https://github.com/peterus/LayerNexus/releases)
with notes grouped into breaking changes, new features and fixes, and the image tags
`<version>`, `<major>.<minor>`, `latest`, `main` and `sha-<commit>`. A build without a
release only updates `main` and `sha-<commit>` — `latest` moves only on releases. Dependency
bumps (`build(deps): …`) therefore reach `latest` with the next release — or right away if
merged with a `fix(deps): …` title.

The running version is shown in the page footer and returned by `GET /health/`
(`{"status": "ok", "version": "0.2.0"}`). Builds between releases report
`<last release>+<commit>`, e.g. `0.2.0+abc1234`.

### Rolling Back

Pin the previous release instead of `latest` (e.g. `image: ghcr.io/peterus/layernexus:0.2.0`)
and recreate the containers. Migrations only run forward: if the newer version migrated the
database, restore the database backup taken before the upgrade as well.

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
    environment:
      - ORCASLICER_PATH=/usr/local/bin/orcaslicer-cli
    volumes:
      - ./docker/orcaslicer-cli.sh:/usr/local/bin/orcaslicer-cli:ro
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

!!! warning "Keep the OrcaSlicer CLI wrapper"
    `docker/orcaslicer-cli.sh` (from this repository) adds `--allow-rotations=0` to every OrcaSlicer call. Without it the OrcaSlicer CLI rotates parts in 45° steps while auto-arranging plates, unlike the OrcaSlicer desktop app.

    Copy the script to `docker/orcaslicer-cli.sh` next to your compose file and make it executable (`chmod +x docker/orcaslicer-cli.sh`). If the file is missing, Docker creates an empty directory at the mount path instead, and every slice fails. In that case check `docker compose logs orcaslicer`, add the script, and recreate the container (`docker compose up -d orcaslicer`).

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
