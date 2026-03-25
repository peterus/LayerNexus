# Configuration

LayerNexus is configured through environment variables. You set these in your `docker-compose.yml` file (under the `environment:` section) or in a separate `.env` file.

!!! info "Docker Image Defaults"
    The Docker image ships with sensible defaults for most settings. For a basic local setup, you only need to set `DJANGO_SECRET_KEY`. Override other values only when needed (e.g., when exposing LayerNexus to the internet).

---

## Essential Settings

| Variable | What It Does | Docker Default |
|---|---|---|
| `DJANGO_SECRET_KEY` | A random string used to secure sessions and forms. **Keep it secret, keep it unique.** | Insecure placeholder — **always change this** |
| `ALLOWED_HOSTS` | The hostnames your server responds to, separated by commas. Add your domain if you access LayerNexus from a different machine. | `localhost,127.0.0.1` |
| `CSRF_TRUSTED_ORIGINS` | Required when using a reverse proxy with HTTPS. Set to your full URL (e.g., `https://layernexus.example.com`). | _(empty)_ |

!!! warning "Always Change the Secret Key"
    The default secret key is not secure. Replace it with a long, random string before exposing LayerNexus to anyone outside your local network.

---

## Optional Settings

These have sensible defaults in the Docker image and usually don't need to be changed:

| Variable | What It Does | Docker Default |
|---|---|---|
| `DEBUG` | Set to `1` to see detailed error pages for troubleshooting. | `0` (off) |
| `ORCASLICER_API_URL` | Where to find the OrcaSlicer API for slicing. | `http://orcaslicer:3000` |
| `SPOOLMAN_URL` | Where to find Spoolman for filament tracking. | `http://spoolman:8000` |
| `ALLOW_REGISTRATION` | Whether new users can create accounts themselves. Set to `true` or `false`. | `true` |
| `LOG_LEVEL` | How much detail to log. Options: `DEBUG`, `INFO`, `WARNING`, `ERROR`. | `INFO` |
| `DATABASE_PATH` | Where to store the database file. You usually don't need to change this. | `/app/data/db.sqlite3` |

---

## Example `.env` File

Instead of listing every variable in `docker-compose.yml`, you can create a `.env` file in the same folder:

```bash
# Required
DJANGO_SECRET_KEY=your-long-random-secret-key-here

# Only needed if you access LayerNexus from other machines
# ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.100

# Only needed if using a reverse proxy with HTTPS
# CSRF_TRUSTED_ORIGINS=https://layernexus.example.com
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
