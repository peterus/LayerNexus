# Spoolman

[Spoolman](https://github.com/Donkie/Spoolman) is a self-hosted filament inventory manager. When connected to LayerNexus, you can pick spools from your inventory when editing parts, and LayerNexus will automatically fill in the color and material type.

---

## Setting It Up

### 1. Add Spoolman to Your Docker Compose

Add the Spoolman service to your `docker-compose.yml`:

```yaml
services:
  web:
    image: ghcr.io/peterus/layernexus:latest
    # ... your existing config ...
    environment:
      - SPOOLMAN_URL=http://spoolman:8000
      # ... other environment variables ...
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

For a complete full-stack example, see [Docker Details](../advanced/docker.md).

### 2. Already Running Spoolman?

If you have Spoolman running elsewhere on your network, just set the environment variable to point to it:

```bash
SPOOLMAN_URL=http://192.168.1.50:7912
```

!!! note
    When `SPOOLMAN_URL` is empty or not set, Spoolman integration is disabled and filament selection won't be available.

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
