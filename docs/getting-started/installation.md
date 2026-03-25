# Installation

This guide walks you through setting up LayerNexus using Docker, which is the recommended deployment method.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) (version 20.10+)
- [Docker Compose](https://docs.docker.com/compose/install/) (v2, included with Docker Desktop)
- A web browser

## Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/peterus/LayerNexus.git
cd LayerNexus
```

### 2. Create an Environment File (Optional)

For development, the defaults are fine. For production, create a `.env` file:

```bash
# .env
DJANGO_SECRET_KEY=your-secret-key-here
ALLOWED_HOSTS=your-domain.com,localhost
CSRF_TRUSTED_ORIGINS=https://your-domain.com
DEBUG=0
```

!!! tip "Generate a Secret Key"
    ```bash
    python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
    ```

### 3. Start the Services

```bash
docker compose up -d
```

This starts two containers:

| Service | Description | Port |
|---|---|---|
| **web** | LayerNexus Django application | `8000` |
| **orcaslicer** | OrcaSlicer API for slicing STL files | `3000` |

The entrypoint script automatically runs database migrations and collects static files on startup.

### 4. Open LayerNexus

Navigate to [http://localhost:8000](http://localhost:8000) in your browser.

### 5. Register the First User

Click **Register** to create your first account.

!!! important "First User Becomes Admin"
    The first user to register is automatically assigned the **Admin** role with full permissions. All subsequent users receive the **Designer** role. See [User Roles](../user-guide/roles.md) for details.

---

## Docker Compose Configuration

The default `docker-compose.yml` included in the repository:

```yaml
services:
  web:
    build:
      context: .
      target: release
    ports:
      - "8000:8000"
    volumes:
      - .:/app
      - media_data:/app/media
      - db_data:/app/data
    environment:
      - DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY:-change-me-in-production}
      - ALLOWED_HOSTS=${ALLOWED_HOSTS:-localhost,127.0.0.1}
      - CSRF_TRUSTED_ORIGINS=${CSRF_TRUSTED_ORIGINS:-}
      - DEBUG=${DEBUG:-1}
      - DATABASE_PATH=/app/data/db.sqlite3
      - ORCASLICER_API_URL=http://orcaslicer:3000
      - SPOOLMAN_URL=${SPOOLMAN_URL:-http://localhost:4000}
      - ALLOW_REGISTRATION=${ALLOW_REGISTRATION:-true}
    command: >
      gunicorn layernexus.wsgi:application
      --bind 0.0.0.0:8000 --workers 3 --reload
    restart: unless-stopped
    depends_on:
      - orcaslicer

  orcaslicer:
    image: ghcr.io/afkfelix/orca-slicer-api:latest-orca2.3.1
    ports:
      - "3000:3000"
    volumes:
      - orcaslicer_data:/app/data
    restart: unless-stopped

volumes:
  media_data:
  db_data:
  orcaslicer_data:
```

!!! note "Development Mode"
    The default configuration mounts the project directory (`.:/app`) as a volume and runs Gunicorn with `--reload`, so code changes are reflected immediately. For production, see [Docker Compose Examples](../deployment/docker-compose.md).

---

## What's Next?

- [Configure environment variables](configuration.md)
- [Follow the first steps guide](first-steps.md)
- [Set up a reverse proxy for production](../deployment/reverse-proxy.md)
