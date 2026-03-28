# Technical Architecture

This page describes the internal architecture of LayerNexus for contributors and developers.

## Tech Stack

| Layer | Technology |
|---|---|
| **Backend** | Django 6.0+ (Python 3.10+) |
| **Frontend** | Bootstrap 5.3 with light/dark mode |
| **Database** | SQLite |
| **3D Viewer** | Three.js |
| **Static Files** | WhiteNoise with compressed manifest storage |
| **Deployment** | Docker + Docker Compose, Gunicorn |
| **CI/CD** | GitHub Actions |
| **External Services** | OrcaSlicer API, Klipper/Moonraker, Bambu Lab Cloud, Spoolman |

---

## Package Structure

```
LayerNexus/
├── manage.py                  # Django management entry point
├── requirements.txt           # Python dependencies
├── pyproject.toml             # Ruff and coverage configuration
├── Dockerfile                 # Multi-stage Docker build
├── docker-compose.yml         # Development services
├── entrypoint.sh              # Container entrypoint (migrations + collectstatic)
├── VERSION                    # Semantic version file
│
├── layernexus/                # Django project package
│   ├── settings.py            # Configuration via environment variables
│   ├── urls.py                # Root URL configuration
│   ├── wsgi.py                # WSGI application
│   └── asgi.py                # ASGI application
│
├── core/                      # Main application
│   ├── models/                # Database models
│   │   ├── projects.py        # Project (hierarchical with sub-projects)
│   │   ├── parts.py           # Part, PrintTimeEstimate
│   │   ├── printers.py        # PrinterProfile, CostProfile, BambuCloudAccount
│   │   ├── printing.py        # PrintJob, PrintJobPart, PrintJobPlate
│   │   ├── queue.py           # PrintQueue
│   │   ├── orca_profiles.py   # OrcaFilamentProfile, OrcaPrintPreset, OrcaMachineProfile
│   │   ├── documents.py       # ProjectDocument, FileVersion
│   │   ├── hardware.py        # HardwarePart, ProjectHardware
│   │   └── spoolman.py        # SpoolmanFilamentMapping
│   │
│   ├── views/                 # Class-based views
│   │   ├── auth.py            # Registration, login, profile
│   │   ├── dashboard.py       # Dashboard and statistics
│   │   ├── projects.py        # Project CRUD
│   │   ├── parts.py           # Part CRUD
│   │   ├── printers.py        # Printer profile management
│   │   ├── print_jobs.py      # Print job management
│   │   ├── queue.py           # Print queue management
│   │   ├── bambuauth.py       # Bambu Lab Cloud authentication wizard
│   │   ├── orca_profiles.py   # OrcaSlicer profile import/management
│   │   ├── documents.py       # Project document upload
│   │   ├── hardware.py        # Hardware catalog and assignment
│   │   ├── materials.py       # Material/filament views
│   │   └── helpers.py         # Shared view utilities
│   │
│   ├── forms/                 # Django ModelForms
│   ├── urls/                  # URL routing (split by feature)
│   ├── services/              # External API clients
│   │   ├── printer_backend.py # PrinterBackend protocol + factory function
│   │   ├── moonraker.py       # Klipper/Moonraker API client
│   │   ├── bambulab.py        # Bambu Lab Cloud/MQTT/FTP client
│   │   ├── orcaslicer.py      # OrcaSlicer API client
│   │   ├── spoolman.py        # Spoolman API client
│   │   ├── profile_import.py  # OrcaSlicer profile import logic
│   │   ├── gcode_thumbnail.py # G-code thumbnail extraction
│   │   └── threemf.py         # 3MF file parsing
│   │
│   ├── templates/             # Django templates
│   │   ├── base.html          # Base layout (Bootstrap 5.3)
│   │   ├── core/              # Application templates
│   │   └── registration/      # Auth templates
│   │
│   ├── templatetags/          # Custom template tags
│   ├── tests/                 # Test suite
│   ├── mixins.py              # RBAC permission mixins
│   ├── admin.py               # Django admin configuration
│   └── context_processors.py  # Template context (app_name, version, registration)
│
├── static/                    # Static assets (CSS, JS, favicons)
└── media/                     # User uploads (runtime, not in VCS)
```

---

## RBAC System

LayerNexus uses Django's built-in **groups and permissions** system for role-based access control.

### Groups

Three Django groups are created automatically:

| Group | Permissions |
|---|---|
| **Admin** | All permissions + `auth.change_user` |
| **Operator** | `can_manage_printers`, `can_control_printer`, `can_manage_print_queue`, `can_dequeue_job`, `can_manage_orca_profiles`, `can_manage_filament_mappings` |
| **Designer** | `can_manage_projects`, `can_manage_print_queue`, `can_dequeue_job` |

### Permission Mixins

All write/delete views use mixins from `core/mixins.py`:

```python
class RoleRequiredMixin(LoginRequiredMixin, PermissionRequiredMixin):
    raise_exception = True  # 403 for authenticated users without permission

class AdminRequiredMixin(RoleRequiredMixin):
    permission_required = "auth.change_user"

class ProjectManageMixin(RoleRequiredMixin):
    permission_required = "core.can_manage_projects"

class PrinterManageMixin(RoleRequiredMixin):
    permission_required = "core.can_manage_printers"

class PrinterControlMixin(RoleRequiredMixin):
    permission_required = "core.can_control_printer"

class OrcaProfileManageMixin(RoleRequiredMixin):
    permission_required = "core.can_manage_orca_profiles"

class FilamentMappingManageMixin(RoleRequiredMixin):
    permission_required = "core.can_manage_filament_mappings"

class QueueManageMixin(RoleRequiredMixin):
    permission_required = "core.can_manage_print_queue"

class QueueDequeueMixin(RoleRequiredMixin):
    permission_required = "core.can_dequeue_job"
```

---

## External Service Clients

All external API integrations are encapsulated in `core/services/`:

### Printer Backend Protocol (`printer_backend.py`)

- Defines the `PrinterBackend` protocol that all printer backends must implement
- Provides `NormalizedJobStatus` dataclass for unified print status across backends
- Factory function `get_printer_backend()` instantiates the correct backend based on `PrinterProfile.printer_type`
- Backends must implement: `get_printer_status()`, `upload_gcode()`, `start_print()`, `get_job_status()`, `cancel_print()`

### OrcaSlicer Client (`orcaslicer.py`)

- Sends STL files and profile JSON to the OrcaSlicer API
- Receives G-code and slicing metadata
- Configured via `ORCASLICER_API_URL`

### Moonraker Client (`moonraker.py`)

- Implements `PrinterBackend` for Klipper printers
- Uploads G-code, starts/pauses/cancels prints, monitors status
- Configured per-printer via `PrinterProfile.moonraker_url`

### Bambu Lab Client (`bambulab.py`)

- Implements `PrinterBackend` for Bambu Lab printers (P1, X1, A1 series)
- Communicates via three channels: Cloud API (status), MQTT (print control), LAN FTP (G-code upload)
- Tokens encrypted at rest using Fernet (AES) derived from Django's `SECRET_KEY`
- Falls back from LAN FTP to Cloud upload, and from MQTT to Cloud API for status

### Spoolman Client (`spoolman.py`)

- Fetches spool inventory and filament details
- Provides filament selection for parts
- Configured via `SPOOLMAN_URL`

### Design Principles

- Each client is a separate class with its own exception type
- All API calls are logged with `logging.getLogger(__name__)`
- Connection errors are caught and surfaced as user-friendly messages
- HTTP requests use the `requests` library

---

## Template System

### Base Template

`core/templates/base.html` provides:

- Bootstrap 5.3 layout with responsive navigation
- Light/dark theme toggle with system preference detection
- Django messages display (success, error, warning, info)
- Favicon and metadata

### Theme Support

The light/dark theme is implemented using Bootstrap's `data-bs-theme` attribute with a JavaScript theme switcher that:

1. Detects system preference via `prefers-color-scheme` media query
2. Allows manual override stored in `localStorage`
3. Applies the theme on page load to prevent flash of unstyled content

---

## Static Files

Static files are served by [WhiteNoise](http://whitenoise.evans.io/) with compressed manifest storage:

```python
STORAGES = {
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}
```

- Static files are collected during Docker build (`collectstatic --noinput`)
- WhiteNoise serves them directly from Django with proper caching headers
- No separate web server (e.g., Nginx) is needed for static files

---

## Database

LayerNexus uses **SQLite** as its database:

- Simple, zero-configuration, file-based
- Stored at `DATABASE_PATH` (default: `/app/data/db.sqlite3`)
- Migrations run automatically via the Docker entrypoint
- Suitable for single-instance deployments

### Key Model Relationships

```
Project (hierarchical, self-referencing)
├── Part → PrintJob → PrintJobPart
├── ProjectDocument → FileVersion
├── ProjectHardware → HardwarePart
└── Sub-Projects (recursive)

PrinterProfile (Klipper or Bambu Lab)
├── CostProfile
└── BambuCloudAccount (for Bambu Lab printers)

PrintJob → PrintQueue (priority-ordered)

OrcaMachineProfile
OrcaFilamentProfile
OrcaPrintPreset

SpoolmanFilamentMapping → Part
```

---

## CI/CD Pipeline

GitHub Actions runs on every push to `main` and on pull requests:

| Job | Description |
|---|---|
| **Lint & Format** | Ruff linter and formatter checks |
| **Tests** | Django system checks, migration checks, full test suite with coverage |
| **Security** | `pip-audit` dependency scan, Django deployment checklist |
| **Docker Build** | Image build and smoke test |

---

## Next Steps

- [Development Guide](contributing.md)
