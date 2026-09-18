# Development Guide

This guide covers everything you need to contribute to LayerNexus — from setting up your development environment to submitting a pull request.

If you just want to **run** LayerNexus, see the [Quick Start](../quick-start.md).

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/)
- [Git](https://git-scm.com/)
- Python 3.10+ (optional, for running linters locally)

---

## Getting Started

```bash
# Clone the repository
git clone https://github.com/peterus/LayerNexus.git
cd LayerNexus

# Start the development environment
docker compose up -d

# The app is now running at http://localhost:8000
```

The `docker-compose.yml` mounts the project directory as a volume, so code changes are immediately reflected. Gunicorn runs with `--reload` for automatic reloading.

| Service | Description | Port |
|---|---|---|
| **web** | LayerNexus (built from local source, auto-reloads on code changes) | `8000` |
| **orcaslicer** | OrcaSlicer API | `3000` |

---

## Building Docker Images

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

## Running Tests

```bash
# Run the full test suite
docker compose exec web python manage.py test core -v 2

# Run with coverage
docker compose exec web coverage run manage.py test core -v 2
docker compose exec web coverage report
```

!!! important
    Always run tests through Docker Compose — never run `manage.py` directly on the host.

---

## Linting and Formatting

LayerNexus uses [Ruff](https://github.com/astral-sh/ruff) for linting and formatting:

```bash
# Check for lint issues
ruff check .

# Check formatting
ruff format --check .

# Auto-fix lint issues
ruff check --fix .

# Auto-format code
ruff format .
```

Ruff is configured in `pyproject.toml`:

- **Line length:** 120 characters
- **Target version:** Python 3.10
- **Excluded:** `core/migrations/`

---

## Code Style

### Strings

- **Always use double quotes** (`"`) — never single quotes (`'`)
- **Always use f-strings** for string interpolation

```python
# ✅ Correct
name = "Alice"
message = f"Hello {name}!"

# ❌ Wrong
name = 'Alice'
message = "Hello " + name + "!"
```

### Type Hints

**Required** on all function signatures and return types:

```python
def calculate_cost(parts: list[Part], multiplier: int = 1) -> float:
    ...
```

### Docstrings

Use **Google-style** docstrings on all public functions and classes:

```python
def slice_part(part: Part, profile: OrcaSlicerProfile) -> Path:
    """Slice a part's STL file using OrcaSlicer API.

    Args:
        part: Part instance with stl_file.
        profile: OrcaSlicer profile to use for slicing.

    Returns:
        Path to the generated G-code file.

    Raises:
        OrcaSlicerError: If slicing fails.
    """
    ...
```

### Views

- **Use class-based views (CBVs) only** — no function-based views
- Use `LoginRequiredMixin` for read-only views
- Use the appropriate role mixin for write/delete views (see [Roles & Permissions](../user-guide/roles.md))
- Use Django's `messages` framework for user feedback

### Models

- Use **Django ORM** exclusively — no raw SQL
- Add `related_name` to all ForeignKey/ManyToMany fields
- Implement `__str__()` for all models
- Add `Meta.ordering` where appropriate

---

## Project Structure

```
core/
├── models/          # Database models (split into modules)
├── views/           # Class-based views (split by feature)
├── forms/           # Model forms
├── urls/            # URL patterns (split by feature)
├── services/        # External API clients
├── templates/       # Django templates (Bootstrap 5.3)
├── templatetags/    # Custom template tags
├── tests/           # Test suite
├── mixins.py        # RBAC mixins
├── admin.py         # Django admin configuration
└── context_processors.py
```

---

## Pull Request Workflow

1. **Fork** the repository.
2. **Create** a feature branch from `main`:
   ```bash
   git checkout -b feature/my-feature
   ```
3. **Make** your changes following the code style guidelines.
4. **Run** linting and tests:
   ```bash
   ruff check . && ruff format --check .
   docker compose exec web python manage.py test core -v 2
   ```
5. **Commit** with a descriptive message.
6. **Push** to your fork and open a Pull Request.

### CI Checks

The CI pipeline runs automatically on pull requests:

| Check | Description |
|---|---|
| **Ruff lint** | Code style and best practices |
| **Ruff format** | Code formatting |
| **Django tests** | Full test suite with coverage |
| **pip-audit** | Dependency security scan |
| **Docker build** | Image build and smoke test |

All checks must pass before a PR can be merged.

---

## Anti-Patterns

| ❌ Avoid | ✅ Use Instead |
|---|---|
| Function-based views | Class-based views |
| Raw SQL queries | Django ORM |
| Single quotes `'` | Double quotes `"` |
| String concatenation | f-strings |
| `LoginRequiredMixin` for writes | Role-specific mixins |
| `request.user.is_staff` | `AdminRequiredMixin` |
| Hardcoded config values | Environment variables |
| Missing type hints | Type hints on all signatures |
| Inline CSS/JavaScript | Static files |

---

## Next Steps

- [Architecture overview](architecture.md)
