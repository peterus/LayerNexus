# Contributing

Contributions to LayerNexus are welcome! This guide covers the development setup, code style, and contribution workflow.

## Development Setup

### Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and [Docker Compose](https://docs.docker.com/compose/install/)
- Python 3.10+ (for running linters locally)
- Git

### Getting Started

```bash
# Clone the repository
git clone https://github.com/peterus/LayerNexus.git
cd LayerNexus

# Start the development environment
docker compose up -d

# The app is now running at http://localhost:8000
```

The development `docker-compose.yml` mounts the project directory as a volume, so code changes are immediately reflected. Gunicorn runs with `--reload` for automatic reloading.

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

### Ruff Configuration

The Ruff configuration is in `pyproject.toml`:

- **Line length:** 120 characters
- **Target version:** Python 3.10
- **Excluded:** `core/migrations/`
- **Enabled rules:** pycodestyle, pyflakes, isort, flake8-bugbear, flake8-comprehensions, pyupgrade, flake8-simplify, flake8-django, flake8-bandit (security), flake8-print

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

## Anti-Patterns to Avoid

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

- [Technical architecture overview](architecture.md)
- [User roles and permissions](../user-guide/roles.md)
