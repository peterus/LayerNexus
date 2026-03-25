# Building from Source

This guide is for developers who want to build LayerNexus from source code instead of using the pre-built Docker image. If you just want to run LayerNexus, see the [Quick Start](../quick-start.md).

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/)
- [Git](https://git-scm.com/)
- Python 3.10+ (optional, for running linters locally)

---

## Clone the Repository

```bash
git clone https://github.com/peterus/LayerNexus.git
cd LayerNexus
```

---

## Start the Development Environment

The repository includes a `docker-compose.yml` that builds from source and mounts the project directory for live reloading:

```bash
docker compose up -d
```

This starts:

| Service | Description | Port |
|---|---|---|
| **web** | LayerNexus (built from local source, auto-reloads on code changes) | `8000` |
| **orcaslicer** | OrcaSlicer API | `3000` |

Open [http://localhost:8000](http://localhost:8000) and register your first user.

---

## Building the Docker Image

### Release Build

```bash
docker build --target release -t layernexus .
```

This builds a production-ready image with static files baked in.

### Debug Build

```bash
docker build --target debug -t layernexus:debug .
```

The debug image includes:

- `debugpy` for remote debugging on port `5678`
- `django-debug-toolbar`
- Django's development server with auto-reload
- `DEBUG=1` by default

### Build Arguments

| Argument | Description | Default |
|---|---|---|
| `APP_VERSION` | Version string baked into the image | `dev` |

---

## Running Tests

```bash
docker compose exec web python manage.py test core -v 2
```

With coverage:

```bash
docker compose exec web coverage run manage.py test core -v 2
docker compose exec web coverage report
```

!!! important
    Always run tests through Docker Compose — never run `manage.py` directly on the host.

---

## Linting and Formatting

LayerNexus uses [Ruff](https://github.com/astral-sh/ruff) for linting and formatting:

```bash
# Check for issues
ruff check .

# Check formatting
ruff format --check .

# Auto-fix issues
ruff check --fix .

# Auto-format code
ruff format .
```

---

## Debug Docker Compose

For development with remote debugging support, build with the `debug` target:

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

Attach your IDE debugger (VS Code or PyCharm) to port `5678` for step-through debugging.

---

## Next Steps

- [Contributing guide](contributing.md)
- [Architecture overview](architecture.md)
