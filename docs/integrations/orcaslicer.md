# OrcaSlicer Integration

LayerNexus uses [OrcaSlicer API](https://github.com/AFKFelix/orca-slicer-api) to slice your STL files into G-code directly from the web interface — no need to open OrcaSlicer on your desktop.

---

## How It Works

1. You upload an STL file to a part in LayerNexus.
2. You pick a slicer profile and click **Slice**.
3. LayerNexus sends the file to the OrcaSlicer API container running alongside it.
4. OrcaSlicer slices the model and sends the G-code back.
5. LayerNexus saves the G-code and shows you the estimated print time and filament usage.

If you followed the [Quick Start](../quick-start.md), the OrcaSlicer API container is already running and connected — no extra setup needed.

---

## Importing Profiles

Before you can slice, you need to import your OrcaSlicer profiles. Three types are required:

- **Machine profile** — your printer hardware settings (build volume, nozzle size, etc.)
- **Filament profile** — filament settings (temperature, flow, cooling)
- **Print preset** — quality and speed settings (layer height, infill, speed)

### How to Import

1. In **OrcaSlicer desktop**, export your profiles as JSON files (from the profile editor).
2. In **LayerNexus**, go to **OrcaSlicer Profiles** in the navigation bar.
3. Click **Import** and upload the three profile files.
4. The profiles are now available when slicing parts.

!!! tip
    You can set a default slicer profile on a project. All parts in that project will use the default profile unless you pick a different one.

---

## Slicing a Part

1. Open a part that has an STL file uploaded.
2. Select the slicer profile (machine + filament + print preset).
3. Click **Slice**.
4. Wait a moment for OrcaSlicer to process the file.
5. When done, you'll see:
    - Estimated print time
    - Filament usage (grams and meters)
    - Layer count

From here, you can [upload the G-code to a printer](moonraker.md) and start printing.

---

## Troubleshooting

If slicing fails, check that the OrcaSlicer container is running:

```bash
docker compose ps orcaslicer
```

If it's not running, restart it:

```bash
docker compose restart orcaslicer
```

---

## Next Steps

- [Upload G-code to printers via Moonraker](moonraker.md)
- [Track filament with Spoolman](spoolman.md)
