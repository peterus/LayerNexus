# CLAUDE.md — LayerNexus

Django 6.0 web app to manage large-scale 3D printing. Core workflow:
**STL upload → OrcaSlicer slicing → G-code to Klipper/Moonraker → print-job & queue tracking.**
Filament inventory via Spoolman. Python 3.10+ (CI: 3.14; local `.venv`: 3.13), SQLite.
App version in `VERSION`.

This is the single source of truth for working in this repo (it replaces the former
`.github/copilot-instructions.md`).

## Commands

Local venv (`.venv`, Python 3.13) — no pytest; use Django's test runner:

```bash
.venv/bin/python manage.py test core                          # full suite (CI runs this)
.venv/bin/python manage.py test core.tests.test_views_parts   # single module
.venv/bin/python manage.py makemigrations                     # after model changes
.venv/bin/python manage.py makemigrations --check --dry-run   # CI gate: fails on missing migration
.venv/bin/python manage.py check --fail-level WARNING
ruff check . && ruff format --check .                         # lint + format (CI gate)
coverage run manage.py test core && coverage report          # coverage (gate: >=55%)
```

Docker (full stack: `web` + `worker` + `orcaslicer` + `spoolman`):

```bash
docker compose up -d
docker compose exec web python manage.py <cmd>
docker compose logs web --tail=50
docker compose logs worker --tail=50
docker compose restart web        # after config changes
```

Code is bind-mounted into the container and gunicorn runs with `--reload`, so code
changes are live. Either the venv or Docker works for management commands; Docker is
required to exercise the OrcaSlicer/Spoolman integrations end to end.

## Structure

The app was refactored from single files into **packages** — do not expect a flat
`models.py` / `views.py` / `forms.py`.

```
core/                      # main Django app
├── models/                # split by domain, re-exported in __init__.py
│   ├── projects.py        #   Project (hierarchical, sub-projects, cover image, multipliers)
│   ├── parts.py           #   Part, PrintTimeEstimate
│   ├── printers.py        #   PrinterProfile, CostProfile
│   ├── printing.py        #   PrintJob, PrintJobPart, PrintJobPlate
│   ├── queue.py           #   PrintQueue
│   ├── documents.py       #   ProjectDocument, FileVersion (uploads, up to 75 MB)
│   ├── hardware.py        #   HardwarePart, ProjectHardware
│   ├── orca_profiles.py   #   OrcaMachine/Filament/PrintPreset profiles
│   └── spoolman.py        #   SpoolmanFilamentMapping
├── views/                 # class-based views, one module per domain
├── forms/                 # ModelForms, one module per domain
├── urls/                  # URL configs, one module per domain (app_name namespaced)
├── services/              # external API clients (see below)
├── management/commands/
│   └── moonraker_worker.py  # background worker process (printer polling / status sync)
├── mixins.py              # RBAC mixins — MANDATORY on every write/delete view
├── templatetags/          # custom template tags
├── templates/core/        # Bootstrap 5.3 templates (light/dark)
└── tests/                 # one test_*.py per area — mirror this when adding code

layernexus/                # settings.py, urls.py, wsgi.py, asgi.py
static/                    # CSS, JS, Three.js 3D viewer, favicon
```

### Services (`core/services/`)

`orcaslicer` (slicing API), `moonraker` + `moonraker_ws` (Klipper control, REST +
WebSocket), `spoolman` (filament), `slicing` / `slicing_worker`, `printer_backend`,
`printer_status_sync`, `profile_import`, `gcode_thumbnail`, `threemf`.

Each integration is its own class with a custom exception (e.g. `MoonrakerError`),
logs via `logging.getLogger(__name__)`, handles connection errors gracefully, and is
tested with `unittest.mock` (never hit the real API in tests).

## Runtime

Two processes: `web` (gunicorn) and `worker` (`manage.py moonraker_worker`, polls
printers / syncs status). Health check: `GET /health/`. Plus two sidecar containers:
`orcaslicer` (`:3000`) and `spoolman` (`:7912`).

## RBAC — mandatory on every write view

Group-based access control. **Every view that modifies data must use a role mixin
from `core/mixins.py`** — `LoginRequiredMixin` alone is only acceptable for read-only
(list/detail) views.

| Role (Django group) | Grants |
|---|---|
| `Admin` | everything + user management (`auth.change_user`) |
| `Operator` | manage/control printers, print queue, dequeue, Orca profiles, filament mappings |
| `Designer` | manage projects, manage queue, dequeue |

First registered user → `Admin`; subsequent self-registrations → `Designer`.

Mixins: `AdminRequiredMixin`, `ProjectManageMixin`, `PrinterManageMixin`,
`PrinterControlMixin`, `OrcaProfileManageMixin`, `FilamentMappingManageMixin`,
`QueueManageMixin`, `QueueDequeueMixin`. Role mixins set `raise_exception = True`
(403, not a redirect, for authenticated-but-unauthorized users).

Never substitute a permission check with `created_by=request.user` filtering or a
raw `request.user.is_staff` check.

## Code style

- **Double quotes only** (strings, docstrings, messages) — never single quotes.
- **f-strings** for interpolation — never `+` concatenation or `%`.
- **Type hints** on all function/method signatures and return types.
- **Google-style docstrings** (English) on public functions, classes, modules.
- Class-based views (never function-based); Django ORM (never raw SQL).
- Config via `os.environ.get()` with sensible defaults — never hardcoded.
- Models: explicit `related_name`, `__str__()`, `Meta.ordering`, core validators.
- Templates: Bootstrap 5.3, extend `base.html`, minimal vanilla JS (no inline CSS/JS).
- Keep business logic in models; use `select_related`/`prefetch_related` to avoid N+1.

## Key patterns

- **Sub-project aggregation:** recurse with a quantity multiplier — see
  `Project._collect_parts_with_multiplier()` (and `_collect_hardware_with_multiplier`,
  `_collect_documents`) as the reference shape.
- **File uploads:** define allowed extensions + max size as constants in the form,
  validate in `clean_<field>()`, use `upload_to='<subfolder>/'`, template needs
  `enctype="multipart/form-data"`. `ProjectDocumentForm` (75 MB, 9 types) is the model.
- **Adding a model:** define in `core/models/<domain>.py` → re-export in
  `models/__init__.py` → `makemigrations` → register in `admin.py` → form → views →
  urls → templates → tests.

## Non-obvious gotchas

- **Ruff is pinned to `0.15.20`** in CI — newer versions reformat Markdown code blocks
  and break `ruff format --check` on the docs. Match it locally. Config in
  `pyproject.toml` (line-length 120; `core/migrations` excluded).
- **Coverage gate: `fail_under = 55`** (`pyproject.toml`).
- **Never hand-edit migrations** — only add via `makemigrations`. CI fails on any
  un-generated migration (`makemigrations --check`).
- **File ownership:** `media/`, `data/`, `staticfiles/` must be owned by the run user.
  Root-owned dirs (e.g. left by Docker) break the test baseline — `chown` them back.
- **`DJANGO_SECRET_KEY` is required when `DEBUG=0`** (raises at startup). Only when
  `DEBUG=1` does it fall back to an insecure dev key.
- `entrypoint.sh` runs `migrate` + `collectstatic` on container start.

## Environment variables

`DJANGO_SECRET_KEY` (required if `DEBUG=0`), `DEBUG`, `ALLOWED_HOSTS`,
`CSRF_TRUSTED_ORIGINS`, `SPOOLMAN_URL`, `ORCASLICER_API_URL`, `ALLOW_REGISTRATION`,
`DATABASE_PATH`, `LOG_LEVEL`, `WORKER_RELOAD_INTERVAL`. See `.env.example`. Document
new vars there and in `docker-compose.yml`.

## CI gate (all must pass before merge)

`ruff check` + `ruff format --check` → `makemigrations --check` →
`manage.py check --fail-level WARNING` → `manage.py test core` (coverage ≥ 55) →
`check --deploy` + `pip-audit` (security) → Docker release build + `/health/` smoke test.
