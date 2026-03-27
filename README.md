# LayerNexus

[![CI](https://github.com/peterus/LayerNexus/actions/workflows/ci.yml/badge.svg)](https://github.com/peterus/LayerNexus/actions/workflows/ci.yml)
[![Docker Image](https://ghcr-badge.egpl.dev/peterus/layernexus/latest_tag?trim=major&label=Docker)](https://github.com/peterus/LayerNexus/pkgs/container/layernexus)
[![Documentation](https://img.shields.io/badge/docs-GitHub%20Pages-blue)](https://peterus.github.io/LayerNexus/)
![License: AGPL v3](https://img.shields.io/badge/License-AGPLv3-blue.svg)

**The control center for your 3D print workflow.**

## Overview

LayerNexus is a Django web application for managing large-scale 3D printing projects. It integrates with **OrcaSlicer** (via API) for slicing, **Klipper/Moonraker** for printer connectivity, and **Spoolman** for filament tracking — giving you a single dashboard to go from STL files to finished prints.

## Features

### Project Management

- 📋 **Projects & Sub-Projects** — Hierarchical project organization with recursive sub-project support and quantity multipliers
- 🖼️ **Cover Images** — Set cover images for projects, displayed in list and detail views (supports clipboard paste)
- 📎 **Project Documents** — Attach files to projects (PDF, TXT, Markdown, PNG, JPG, SVG, STEP, DXF — up to 75 MB)
- 🔧 **Hardware Catalog** — Reusable hardware component catalog (screws, nuts, bolts, motors, electronics, etc.) with per-project assignments and cost tracking

### 3D Printing Workflow

- 📁 **STL File Upload & 3D Viewer** — Upload STL files and preview models in the browser with Three.js
- 🎨 **Part Organization** — Track color, material, and quantity per part
- 📏 **Filament Calculation** — Estimate filament usage per part and per project (including sub-projects)
- 🔪 **OrcaSlicer API Integration** — Automated slicing via orca-slicer-api Docker container
- ⚙️ **OrcaSlicer Profile Management** — Import and reuse machine, filament, and print preset profiles
- 📤 **Upload to Klipper** — Send G-code directly via the Moonraker API
- 📊 **Print Status Tracking** — Monitor print progress from Klipper

### Filament & Inventory

- 🧵 **Filament Selection via Spoolman** — Pick filament types and track inventory

### Queue & Cost Management

- 🖨️ **Multi-Printer Queue** — Priority-based print queue management across multiple printers
- 💰 **Cost Calculation** — Per-project cost breakdown (filament, electricity, depreciation, maintenance, hardware)
- ⏱️ **Time Estimation** — Historical data calibration for more accurate print time estimates

### Platform

- 👤 **User Management** — Registration, login, profiles, and role-based access control
- 🔐 **Role-Based Access Control** — Admin, Operator, and Designer roles with fine-grained permissions
- 📈 **Dashboard & Statistics** — Overview widgets, material breakdowns, job status charts
- 🌗 **Light & Dark Theme** — Bootstrap-powered theme switching with system preference detection
- 🐳 **Docker Support** — Dockerfile and docker-compose for easy deployment
- ✅ **CI/CD** — GitHub Actions workflow for linting, testing, security audits, and Docker builds

## Tech Stack

| Layer | Technology |
|---|---|
| **Backend** | Django 6.0+ (Python 3.10+) |
| **Frontend** | Bootstrap 5.3 with light/dark mode |
| **Database** | SQLite (default) |
| **3D Viewer** | Three.js (r160, ES modules) |
| **Static Files** | WhiteNoise with compressed manifest storage |
| **Deployment** | Docker + Docker Compose, Gunicorn |
| **CI/CD** | GitHub Actions (ruff lint/format, tests, pip-audit, Docker smoke test) |
| **External Services** | orca-slicer-api, Klipper/Moonraker, Spoolman |

## Quick Start

### Option 1: Docker (recommended)

```bash
# Clone the repository
git clone https://github.com/peterus/LayerNexus.git
cd LayerNexus

# Start with Docker Compose (migrations run automatically on startup)
docker compose up -d

# Create admin user
docker compose exec web python manage.py createsuperuser
```

Then open [http://localhost:8000](http://localhost:8000) in your browser.

### Option 2: Local Development

```bash
# Clone the repository
git clone https://github.com/peterus/LayerNexus.git
cd LayerNexus

# Install dependencies
pip install -r requirements.txt

# Run migrations
python manage.py migrate

# Collect static files
python manage.py collectstatic --noinput

# Create admin user
python manage.py createsuperuser

# Run the development server
python manage.py runserver
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000) in your browser.

## Project Structure

```
LayerNexus/
├── manage.py
├── requirements.txt
├── pyproject.toml          # Ruff configuration
├── Dockerfile
├── docker-compose.yml
├── entrypoint.sh           # Docker entrypoint (migrations + collectstatic)
├── gunicorn.ctl            # Gunicorn config
├── .github/
│   ├── workflows/ci.yml    # CI pipeline
│   └── copilot-instructions.md
├── layernexus/             # Django project settings
│   ├── settings.py
│   ├── urls.py
│   ├── wsgi.py
│   └── asgi.py
├── core/                   # Main application
│   ├── models.py           # 17 models
│   ├── views.py            # 73 class-based views
│   ├── urls.py
│   ├── forms.py
│   ├── mixins.py           # Role-based access control mixins
│   ├── admin.py
│   ├── context_processors.py
│   ├── tests.py            # 319 tests
│   ├── services/
│   │   ├── moonraker.py    # Klipper/Moonraker API client
│   │   ├── orcaslicer.py   # orca-slicer-api REST client
│   │   └── spoolman.py     # Spoolman API client
│   ├── templates/
│   │   ├── base.html       # Base template with favicon, navbar, theme switcher
│   │   ├── core/           # 46 app templates
│   │   └── registration/   # Auth templates (login, register, profile)
│   └── templatetags/
│       └── core_tags.py    # Custom template tags
├── static/
│   ├── css/custom.css
│   ├── favicon.svg         # SVG favicon (stacked layers design)
│   ├── favicon-32.png      # 32×32 PNG favicon
│   └── favicon-180.png     # Apple touch icon (180×180)
└── media/                  # User uploads (STL, G-code, documents, images)
```

## Configuration

LayerNexus is configured through environment variables or directly in `layernexus/settings.py`.

| Variable | Description | Default |
|---|---|---|
| `DJANGO_SECRET_KEY` | Secret key for cryptographic signing | Auto-generated in development |
| `ALLOWED_HOSTS` | Comma-separated list of allowed hostnames | `localhost,127.0.0.1` |
| `DEBUG` | Enable debug mode (`1` or `0`) | `1` |
| `DATABASE_PATH` | Path to SQLite database file | `db.sqlite3` |
| `ORCASLICER_API_URL` | URL of the orca-slicer-api service | `http://localhost:3000` |
| `SPOOLMAN_URL` | URL of the Spoolman instance (required for filament management) | `` |
| `ALLOW_REGISTRATION` | Allow new users to self-register (`true` or `false`) | `true` |

## External Services Setup

### OrcaSlicer API

LayerNexus uses [orca-slicer-api](https://github.com/AFKFelix/orca-slicer-api) — a REST API wrapper for OrcaSlicer that runs as a separate Docker container (included in `docker-compose.yml`).

1. The `docker-compose.yml` already includes the `orcaslicer` service (`ghcr.io/afkfelix/orca-slicer-api:latest-orca2.3.1`).
2. When running via Docker Compose, slicing is available automatically — no additional setup needed.
3. Import your slicer profiles (machine, filament, print preset) through the LayerNexus UI.

### Moonraker (Klipper)

1. Make sure [Moonraker](https://github.com/Arksine/moonraker) is running alongside your Klipper installation.
2. Configure the Moonraker URL (e.g., `http://<printer-ip>:7125`) in the LayerNexus printer settings.
3. Ensure LayerNexus can reach the Moonraker API over your network.

### Spoolman

1. Install and run [Spoolman](https://github.com/Donkie/Spoolman).
2. Set the `SPOOLMAN_URL` environment variable (e.g., `http://<host>:7912`).
3. Spoolman is the primary source for filament data — materials are managed exclusively through Spoolman.


## User Roles & Permissions

LayerNexus uses a group-based role system with three built-in roles:

| Role | Description | Key Permissions |
|---|---|---|
| **Admin** | Full access, user management | All permissions + user CRUD |
| **Operator** | Printer control and queue management | Manage printers, control printers, manage queue, manage OrcaSlicer profiles, manage filament mappings |
| **Designer** | Project and part management | Manage projects/parts, manage print queue |

The first user to register is automatically assigned the **Admin** role. Subsequent self-registered users receive the **Designer** role. Admins can change user roles via the User Management section.

## Data Models

### Core Models

| Model | Description |
|---|---|
| **Project** | Top-level or sub-project with optional cover image, default slicer profile, and recursive sub-project support |
| **Part** | A printable part with STL file, filament requirements, and Spoolman filament linking |
| **PrintJob** | A print job for a specific part, tracking status from creation through completion |
| **PrintQueue** | Priority-ordered queue linking print jobs to printers |

### Printer & Slicing Models

| Model | Description |
|---|---|
| **PrinterProfile** | Printer configuration with Moonraker URL and API key |
| **PrinterCostProfile** | Cost parameters (electricity, depreciation, maintenance) per printer |
| **OrcaSlicerProfile** | Slicer profile bundle (machine, filament, print preset config files) |

### Project Attachments

| Model | Description |
|---|---|
| **ProjectDocument** | File attachments (PDF, images, CAD files, etc.) linked to a project |
| **HardwarePart** | Reusable hardware catalog with 10 categories and optional pricing |
| **ProjectHardware** | Links hardware parts to projects with quantities and project-specific notes |

## Running Tests

```bash
# Via Docker (recommended)
docker compose exec web python manage.py test core

# Local
python manage.py test core
```

The test suite includes 319 tests covering models, views, forms, services, permissions, and integration features.

## CI/CD Pipeline

The GitHub Actions CI pipeline runs on every push to `main` and on pull requests:

| Job | Description |
|---|---|
| **Lint & Format** | Ruff linter and formatter checks |
| **Tests** | Django system checks, migration checks, full test suite with coverage |
| **Security** | pip-audit dependency scan, Django deployment checklist |
| **Docker Build** | Image build and smoke test (container starts + responds on `/accounts/login/`) |

## 🚀 Future Ideas

- 🔔 Notification system (email, Telegram, Discord) for print completion/failure
- 🔍 Print failure detection with OctoPrint/Obico integration
- 💧 Filament drying reminders based on material and storage time
- 📦 Import/export projects as ZIP archives
- 🔌 REST API for mobile app or external integrations
- 🏷️ QR code labels for parts and spools
- 🌐 Integration with Thingiverse/Printables for importing models
- 🎬 Timelapse management linking videos to completed prints
- 🔧 Printer maintenance logging and reminders
- 🔪 Support for multiple slicer backends (PrusaSlicer, Cura)
- 📱 Progressive Web App (PWA) support
- 📊 Interactive charts with Chart.js for historical statistics
- 🗂️ Project templates for common assemblies

## License

This project is licensed under the **GNU Affero General Public License v3.0** — see the [LICENSE](LICENSE) file for details.

## Contributing

Contributions are welcome! To get started:

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Commit your changes (`git commit -m 'Add my feature'`)
4. Push to the branch (`git push origin feature/my-feature`)
5. Open a Pull Request

Please make sure your changes pass linting (`ruff check . && ruff format --check .`) and tests before submitting.
