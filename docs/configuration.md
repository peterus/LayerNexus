# Configuration

LayerNexus is configured through environment variables. You set these in your `docker-compose.yml` file (under the `environment:` section) or in a separate `.env` file.

---

## Essential Settings

These are the settings you should always configure:

| Variable | What It Does | Default |
|---|---|---|
| `DJANGO_SECRET_KEY` | A random string used to secure sessions and forms. **Keep it secret, keep it unique.** | Auto-generated (insecure) |
| `ALLOWED_HOSTS` | The hostnames your server responds to, separated by commas. | `localhost,127.0.0.1` |
| `DEBUG` | Set to `0` for normal use. Only set to `1` if you're troubleshooting. | `1` |
| `CSRF_TRUSTED_ORIGINS` | Required when using a reverse proxy with HTTPS. Set to your full URL (e.g., `https://layernexus.example.com`). | _(empty)_ |

!!! warning "Always Change the Secret Key"
    The default secret key is not secure. Replace it with a long, random string before exposing LayerNexus to anyone outside your local network.

---

## Optional Settings

| Variable | What It Does | Default |
|---|---|---|
| `ORCASLICER_API_URL` | Where to find the OrcaSlicer API for slicing. If you use the docker-compose from the [Quick Start](quick-start.md), this is set automatically. | `http://localhost:3000` |
| `SPOOLMAN_URL` | URL of your Spoolman instance for filament tracking. Leave empty to disable. | _(empty — disabled)_ |
| `ALLOW_REGISTRATION` | Whether new users can create accounts themselves. Set to `true` or `false`. | `true` |
| `LOG_LEVEL` | How much detail to log. Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`. | `INFO` (or `DEBUG` when `DEBUG=1`) |
| `DATABASE_PATH` | Where to store the database file. You usually don't need to change this. | `/app/data/db.sqlite3` |

---

## Example `.env` File

Instead of listing every variable in `docker-compose.yml`, you can create a `.env` file in the same folder:

```bash
# Essential
DJANGO_SECRET_KEY=your-long-random-secret-key-here
DEBUG=0
ALLOWED_HOSTS=localhost,127.0.0.1

# If using a reverse proxy with HTTPS
# CSRF_TRUSTED_ORIGINS=https://layernexus.example.com

# Integrations
ORCASLICER_API_URL=http://orcaslicer:3000
# SPOOLMAN_URL=http://spoolman:8000

# User registration
ALLOW_REGISTRATION=true
```

Then reference it in your `docker-compose.yml`:

```yaml
services:
  web:
    image: ghcr.io/peterus/layernexus:latest
    env_file:
      - .env
    # ... rest of config
```

---

## Data Volumes

LayerNexus stores its data in two locations inside the container. Make sure these are backed by Docker volumes (as shown in the [Quick Start](quick-start.md)) so your data survives container restarts:

| Container Path | What's Stored There |
|---|---|
| `/app/data/` | The database (all your projects, users, settings) |
| `/app/media/` | Uploaded files (STL files, G-code, images, documents) |

!!! danger "Don't Skip Volumes"
    Without volumes, all your data is lost when the container is recreated. The Quick Start docker-compose already sets these up for you.

---

## Next Steps

- [First Steps — create your first project](user-guide/first-steps.md)
- [Connect Spoolman for filament tracking](integrations/spoolman.md)
- [Set up HTTPS with a reverse proxy](advanced/reverse-proxy.md)
