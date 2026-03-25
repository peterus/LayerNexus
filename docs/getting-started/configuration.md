# Configuration

LayerNexus is configured through environment variables. These can be set in your shell, in a `.env` file, or directly in `docker-compose.yml`.

## Environment Variables

### Core Settings

| Variable | Description | Default | Required |
|---|---|---|---|
| `DJANGO_SECRET_KEY` | Secret key for cryptographic signing (sessions, CSRF tokens). **Must be unique and secret in production.** | Auto-generated insecure key | **Yes** (production) |
| `DEBUG` | Enable debug mode. Set to `1` for development, `0` for production. | `1` | No |
| `ALLOWED_HOSTS` | Comma-separated list of hostnames the server responds to. | `localhost,127.0.0.1` | **Yes** (production) |
| `CSRF_TRUSTED_ORIGINS` | Comma-separated list of trusted origins for CSRF protection. Required when behind a reverse proxy. | _(empty)_ | **Yes** (reverse proxy) |

!!! danger "Secret Key in Production"
    Never use the default secret key in production. Generate a secure key:

    ```bash
    python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
    ```

### Database

| Variable | Description | Default |
|---|---|---|
| `DATABASE_PATH` | Absolute path to the SQLite database file. | `/app/data/db.sqlite3` (Docker) or `db.sqlite3` (local) |

The database file is created automatically on first startup. In Docker, it is stored in the `db_data` named volume at `/app/data/db.sqlite3`.

### External Services

| Variable | Description | Default |
|---|---|---|
| `ORCASLICER_API_URL` | URL of the [OrcaSlicer API](https://github.com/AFKFelix/orca-slicer-api) service. | `http://localhost:3000` |
| `SPOOLMAN_URL` | URL of the [Spoolman](https://github.com/Donkie/Spoolman) instance for filament tracking. Leave empty to disable. | _(empty)_ |

!!! tip "Docker Compose Networking"
    When using Docker Compose, the OrcaSlicer API is available at `http://orcaslicer:3000` using Docker's internal DNS. This is already configured in the default `docker-compose.yml`.

### Authentication

| Variable | Description | Default |
|---|---|---|
| `ALLOW_REGISTRATION` | Allow new users to self-register. Set to `true`, `1`, or `yes` to enable; any other value disables registration. | `true` |

### Logging

| Variable | Description | Default |
|---|---|---|
| `LOG_LEVEL` | Log level for the `core` application logger. Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`. | `DEBUG` when `DEBUG=1`, otherwise `INFO` |

### Application Metadata

| Variable | Description | Default |
|---|---|---|
| `APP_VERSION` | Application version string, displayed in the UI. Automatically set from the `VERSION` file or the Docker build argument. | Read from `VERSION` file (currently `0.1.0`) |

---

## Example `.env` File

```bash
# Production environment
DJANGO_SECRET_KEY=your-generated-secret-key-here
DEBUG=0
ALLOWED_HOSTS=layernexus.example.com,localhost
CSRF_TRUSTED_ORIGINS=https://layernexus.example.com

# Database (Docker default is fine in most cases)
DATABASE_PATH=/app/data/db.sqlite3

# External services
ORCASLICER_API_URL=http://orcaslicer:3000
SPOOLMAN_URL=http://spoolman:7912

# Authentication
ALLOW_REGISTRATION=true

# Logging
LOG_LEVEL=INFO
```

---

## Docker-Specific Notes

### Volume Mounts

The Docker image expects two persistent volumes:

| Path | Purpose |
|---|---|
| `/app/data/` | SQLite database file |
| `/app/media/` | User-uploaded files (STL, G-code, images, documents) |

!!! warning "Data Persistence"
    Always use named volumes or bind mounts for `/app/data/` and `/app/media/`. Without them, data is lost when the container is recreated. See [Backup & Restore](../deployment/backup.md) for backup strategies.

### Build Arguments

The Dockerfile accepts the following build argument:

| Argument | Description | Default |
|---|---|---|
| `APP_VERSION` | Version string baked into the image. Set automatically by the CI pipeline. | `dev` |

---

## Next Steps

- [First Steps — create your first project](first-steps.md)
- [Docker Compose examples for production](../deployment/docker-compose.md)
