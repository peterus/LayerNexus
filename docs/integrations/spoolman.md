# Spoolman

LayerNexus integrates with [Spoolman](https://github.com/Donkie/Spoolman) for filament inventory management, allowing you to track spool usage across parts and projects.

## What is Spoolman?

[Spoolman](https://github.com/Donkie/Spoolman) is a self-hosted filament inventory manager. It tracks:

- Spool inventory (weight remaining, location)
- Filament types and materials (PLA, PETG, ABS, etc.)
- Manufacturer and color information
- Usage history

LayerNexus uses Spoolman as the **primary source** for filament data — materials are managed exclusively through Spoolman.

---

## Configuration

### Environment Variable

| Variable | Description | Default |
|---|---|---|
| `SPOOLMAN_URL` | URL of the Spoolman instance | _(empty — disabled)_ |

Set this to the URL where your Spoolman instance is running:

```bash
SPOOLMAN_URL=http://spoolman:8000     # Docker Compose internal
SPOOLMAN_URL=http://192.168.1.50:7912 # External Spoolman instance
```

!!! note
    When `SPOOLMAN_URL` is empty or not set, Spoolman integration is disabled. Filament selection features will not be available.

### Docker Compose

To run Spoolman alongside LayerNexus, add it to your `docker-compose.yml`:

```yaml
services:
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

Then set `SPOOLMAN_URL=http://spoolman:8000` in the LayerNexus service environment.

See [Docker Compose Examples](../deployment/docker-compose.md) for a complete full-stack configuration.

---

## Filament Mapping

Spoolman filament mappings link Spoolman spools to parts in LayerNexus.

### How It Works

1. Spoolman manages your filament inventory (spools, materials, colors).
2. In LayerNexus, when editing a part, you can select a filament from Spoolman.
3. LayerNexus fetches available spools from the Spoolman API and displays them for selection.
4. When a spool is selected, the part automatically inherits:
    - **Color** from the spool
    - **Material type** (PLA, PETG, ABS, etc.)

### Setting Up Filament Mappings

1. Ensure Spoolman is running and has spools configured.
2. In LayerNexus, set `SPOOLMAN_URL` to point to your Spoolman instance.
3. Open a part for editing.
4. In the filament selection field, choose a spool from the Spoolman dropdown.
5. The color and material fields are automatically populated.

---

## Automatic Color and Material Population

When you select a Spoolman spool for a part:

- The **color** is set from the spool's color hex code.
- The **material** is set from the spool's filament type.

This ensures consistency between your filament inventory and your print projects.

!!! tip
    Keep your Spoolman inventory up to date. As you use filament, update spool weights in Spoolman so LayerNexus reflects accurate availability.

---

## Troubleshooting

### Spoolman Connection Fails

- Verify the `SPOOLMAN_URL` is correct and the Spoolman instance is running.
- Test connectivity: `curl http://<spoolman-url>/api/v1/spool`
- If running in Docker, ensure both services are on the same Docker network.

### No Spools Available

- Check that you have configured spools in Spoolman.
- Verify the Spoolman API returns data: `curl http://<spoolman-url>/api/v1/spool`

---

## Next Steps

- [OrcaSlicer integration](orcaslicer.md)
- [Klipper / Moonraker integration](moonraker.md)
- [Manage parts and filament in projects](../user-guide/projects.md)
