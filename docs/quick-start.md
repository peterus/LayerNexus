# Quick Start

Get LayerNexus up and running in under 5 minutes. This guide uses the pre-built Docker image — no need to download source code or build anything.

---

## What You Need

- **Docker** and **Docker Compose** installed on your computer.
  If you don't have them yet, follow the [official Docker install guide](https://docs.docker.com/get-docker/). Docker Desktop (Windows/Mac) or Docker Engine (Linux) both work.

!!! info "What is Docker?"
    Docker runs applications in lightweight containers — think of it as a box that has everything the app needs, so you don't have to install Python, databases, or other dependencies yourself. You just start the container and it works.

---

## 1. Create a Project Folder

Open a terminal and create a folder for LayerNexus:

```bash
mkdir layernexus
cd layernexus
```

---

## 2. Create a `docker-compose.yml` File

Create a file called `docker-compose.yml` in your new folder with this content:

```yaml
services:
  web:
    image: ghcr.io/peterus/layernexus:latest
    ports:
      - "8000:8000"
    volumes:
      - layernexus_data:/app/data
      - layernexus_media:/app/media
    environment:
      - DJANGO_SECRET_KEY=change-me-to-something-random
    restart: unless-stopped
    depends_on:
      - orcaslicer
      - spoolman

  orcaslicer:
    image: ghcr.io/afkfelix/orca-slicer-api:latest-orca2.3.1
    restart: unless-stopped

  spoolman:
    image: ghcr.io/donkie/spoolman:latest
    ports:
      - "7912:8000"
    volumes:
      - spoolman_data:/home/app/.local/share/spoolman
    restart: unless-stopped

volumes:
  layernexus_data:
  layernexus_media:
  spoolman_data:
```

!!! tip "Secret Key"
    Replace `change-me-to-something-random` with any long, random string. This is used to keep your sessions secure. A password generator works great for this.

!!! info "Sensible Defaults"
    The Docker image already includes sensible defaults for `ALLOWED_HOSTS`, `ORCASLICER_API_URL`, `SPOOLMAN_URL`, and more. You only need to set `DJANGO_SECRET_KEY`. See [Configuration](configuration.md) for all available settings.

---

## 3. Start LayerNexus

```bash
docker compose up -d
```

Docker will download the images (this may take a minute the first time) and start everything in the background. The database is set up automatically on first launch.

---

## 4. Open LayerNexus

Go to [http://localhost:8000](http://localhost:8000) in your browser.

---

## 5. Create Your Account

Click **Register** and create your first account.

!!! important "First User = Admin"
    The very first account you create automatically becomes the **Admin** with full permissions. Everyone who registers after that gets the **Designer** role. You can change roles later — see [Roles & Permissions](user-guide/roles.md).

---

## Next Steps

- :material-book-open-variant: **[First Steps](user-guide/first-steps.md)** — Create your first project and upload parts
- :material-cog: **[Configuration](configuration.md)** — Customize settings like registration, Spoolman, and more
- :material-connection: **[Integrations](integrations/orcaslicer.md)** — Learn how to use OrcaSlicer, Moonraker, and Spoolman
