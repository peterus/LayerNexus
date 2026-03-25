# Spoolman

[Spoolman](https://github.com/Donkie/Spoolman) is a self-hosted filament inventory manager. When connected to LayerNexus, you can pick spools from your inventory when editing parts, and LayerNexus will automatically fill in the color and material type.

---

## Setting It Up

### 1. Add Spoolman to Your Docker Compose

If you haven't already (the [Quick Start](../quick-start.md) includes Spoolman by default), add the Spoolman service to your `docker-compose.yml`:

```yaml
services:
  web:
    image: ghcr.io/peterus/layernexus:latest
    # ... your existing config ...
    depends_on:
      - spoolman

  spoolman:
    image: ghcr.io/donkie/spoolman:latest
    ports:
      - "7912:8000"
    volumes:
      - spoolman_data:/home/app/.local/share/spoolman
    restart: unless-stopped

volumes:
  spoolman_data:
```

The Docker image already defaults `SPOOLMAN_URL` to `http://spoolman:8000`, so as long as your Spoolman service is named `spoolman`, no extra configuration is needed.

### 2. Already Running Spoolman?

If you have Spoolman running elsewhere on your network, just set the environment variable to point to it:

```bash
SPOOLMAN_URL=http://192.168.1.50:7912
```

---

## Using Spoolman with Parts

Once Spoolman is connected:

1. Make sure you have spools set up in Spoolman (with colors, materials, etc.).
2. In LayerNexus, open a part for editing.
3. In the filament selection field, pick a spool from the dropdown.
4. The **color** and **material** fields are automatically filled in from the spool data.

This keeps your filament data consistent between Spoolman and your print projects.

!!! tip
    Keep your Spoolman inventory up to date. As you use filament, update spool weights in Spoolman so LayerNexus reflects accurate availability.

---

## Troubleshooting

**Can't see any spools?**

- Check that you have spools configured in Spoolman.
- Verify the connection: `curl http://<spoolman-url>/api/v1/spool`
- If running in Docker, make sure both services are on the same Docker network.

**Connection fails?**

- Double-check the `SPOOLMAN_URL` value.
- Make sure the Spoolman container is running: `docker compose ps spoolman`

---

## Next Steps

- [OrcaSlicer integration](orcaslicer.md)
- [Klipper / Moonraker integration](moonraker.md)
- [Managing parts in projects](../user-guide/projects.md)
