# Docker Compose Examples

This page provides Docker Compose configurations for different deployment scenarios.

## Basic Setup

The minimal setup with LayerNexus and the OrcaSlicer API:

```yaml
services:
  web:
    image: ghcr.io/peterus/layernexus:latest
    ports:
      - "8000:8000"
    volumes:
      - db_data:/app/data
      - media_data:/app/media
    environment:
      - DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY:-change-me-in-production}
      - ALLOWED_HOSTS=${ALLOWED_HOSTS:-localhost,127.0.0.1}
      - DEBUG=${DEBUG:-1}
      - ORCASLICER_API_URL=http://orcaslicer:3000
    restart: unless-stopped
    depends_on:
      - orcaslicer

  orcaslicer:
    image: ghcr.io/afkfelix/orca-slicer-api:latest-orca2.3.1
    volumes:
      - orcaslicer_data:/app/data
    restart: unless-stopped

volumes:
  db_data:
  media_data:
  orcaslicer_data:
```

---

## Production Setup

A hardened configuration with proper secrets and no debug mode:

```yaml
services:
  web:
    image: ghcr.io/peterus/layernexus:latest
    ports:
      - "8000:8000"
    volumes:
      - db_data:/app/data
      - media_data:/app/media
    env_file:
      - .env
    environment:
      - DATABASE_PATH=/app/data/db.sqlite3
      - ORCASLICER_API_URL=http://orcaslicer:3000
    restart: unless-stopped
    depends_on:
      - orcaslicer

  orcaslicer:
    image: ghcr.io/afkfelix/orca-slicer-api:latest-orca2.3.1
    volumes:
      - orcaslicer_data:/app/data
    restart: unless-stopped

volumes:
  db_data:
  media_data:
  orcaslicer_data:
```

With a `.env` file:

```bash
# .env
DJANGO_SECRET_KEY=your-generated-secret-key-here
DEBUG=0
ALLOWED_HOSTS=layernexus.example.com
CSRF_TRUSTED_ORIGINS=https://layernexus.example.com
ALLOW_REGISTRATION=false
LOG_LEVEL=INFO
```

!!! tip "Generate a Secret Key"
    ```bash
    python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
    ```

---

## Full Stack (with Spoolman)

A complete setup including Spoolman for filament inventory management:

```yaml
services:
  web:
    image: ghcr.io/peterus/layernexus:latest
    ports:
      - "8000:8000"
    volumes:
      - db_data:/app/data
      - media_data:/app/media
    env_file:
      - .env
    environment:
      - DATABASE_PATH=/app/data/db.sqlite3
      - ORCASLICER_API_URL=http://orcaslicer:3000
      - SPOOLMAN_URL=http://spoolman:8000
    restart: unless-stopped
    depends_on:
      - orcaslicer
      - spoolman

  orcaslicer:
    image: ghcr.io/afkfelix/orca-slicer-api:latest-orca2.3.1
    volumes:
      - orcaslicer_data:/app/data
    restart: unless-stopped

  spoolman:
    image: ghcr.io/donkie/spoolman:latest
    ports:
      - "7912:8000"
    volumes:
      - spoolman_data:/home/app/.local/share/spoolman
    restart: unless-stopped

volumes:
  db_data:
  media_data:
  orcaslicer_data:
  spoolman_data:
```

!!! note "Moonraker"
    Moonraker runs on your 3D printer, not as a Docker container alongside LayerNexus. Configure the Moonraker URL (e.g., `http://192.168.1.100:7125`) in the LayerNexus printer profile settings. See [Klipper / Moonraker integration](../integrations/moonraker.md).

---

## Debug Mode

For development with remote debugging support:

```yaml
services:
  web:
    build:
      context: .
      target: debug
    ports:
      - "8000:8000"
      - "5678:5678"
    volumes:
      - .:/app
      - media_data:/app/media
      - db_data:/app/data
    environment:
      - DJANGO_SECRET_KEY=dev-secret-key
      - DEBUG=1
      - ORCASLICER_API_URL=http://orcaslicer:3000
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

The debug target:

- Uses `debugpy` on port `5678` for remote debugging (attach with VS Code or PyCharm)
- Runs Django's development server with auto-reload
- Mounts the source code directory for live editing
- Installs `django-debug-toolbar`

---

## Next Steps

- [Reverse proxy configuration](reverse-proxy.md)
- [Backup & restore](backup.md)
