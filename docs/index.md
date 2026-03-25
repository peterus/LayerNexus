# LayerNexus

**The control center for your 3D print workflow.**

LayerNexus is a Django web application for managing large-scale 3D printing projects. It integrates with [OrcaSlicer API](https://github.com/AFKFelix/orca-slicer-api) for slicing, [Klipper/Moonraker](https://github.com/Arksine/moonraker) for printer control, and [Spoolman](https://github.com/Donkie/Spoolman) for filament tracking — giving you a single dashboard to go from STL files to finished prints.

<!-- Screenshot placeholder: add a screenshot of the dashboard at docs/assets/screenshot-dashboard.png -->
<!-- ![LayerNexus Dashboard](assets/screenshot-dashboard.png) -->

---

## Key Features

- :material-folder-multiple: **Projects & Sub-Projects** — Hierarchical project organization with quantity multipliers and cost aggregation
- :material-cube-outline: **STL Upload & 3D Viewer** — Upload STL files and preview models in the browser with Three.js
- :material-content-cut: **OrcaSlicer Integration** — Automated slicing via the OrcaSlicer API Docker container
- :material-printer-3d: **Printer Control** — Send G-code to Klipper printers via Moonraker and monitor print status
- :material-spool: **Filament Tracking** — Select filament from Spoolman and track material usage per part
- :material-format-list-numbered: **Print Queue** — Priority-based multi-printer queue management
- :material-currency-usd: **Cost Estimation** — Per-project cost breakdown including filament, electricity, depreciation, and hardware
- :material-file-document-multiple: **Project Documents** — Attach PDF, images, CAD files, and more to projects
- :material-wrench: **Hardware Catalog** — Reusable catalog of screws, nuts, bolts, electronics, and other components
- :material-shield-account: **Role-Based Access Control** — Admin, Operator, and Designer roles with fine-grained permissions
- :material-theme-light-dark: **Light & Dark Theme** — Bootstrap 5.3 with automatic system preference detection

---

## Quick Links

<div class="grid cards" markdown>

- :material-rocket-launch: **[Getting Started](getting-started/installation.md)**

    Install LayerNexus with Docker in minutes.

- :material-docker: **[Docker Deployment](deployment/docker.md)**

    Production deployment with Docker and Docker Compose.

- :material-connection: **[Integrations](integrations/orcaslicer.md)**

    Connect OrcaSlicer, Moonraker, and Spoolman.

- :material-account-group: **[User Roles](user-guide/roles.md)**

    Understand the Admin, Operator, and Designer roles.

</div>

---

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
| **External Services** | OrcaSlicer API, Klipper/Moonraker, Spoolman |
