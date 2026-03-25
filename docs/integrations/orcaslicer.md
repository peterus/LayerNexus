# OrcaSlicer Integration

LayerNexus integrates with [OrcaSlicer API](https://github.com/AFKFelix/orca-slicer-api) to provide automated slicing of STL files into G-code directly from the web interface.

## What is OrcaSlicer API?

[OrcaSlicer API](https://github.com/AFKFelix/orca-slicer-api) is a REST API wrapper around OrcaSlicer that runs as a separate Docker container. It accepts STL files and slicer profiles, performs the slicing operation, and returns the resulting G-code with metadata (print time estimates, filament usage, etc.).

---

## How It Works

```
┌─────────────┐    STL + Profiles    ┌──────────────────┐
│  LayerNexus  │ ──────────────────► │  OrcaSlicer API  │
│  (web)       │ ◄────────────────── │  (orcaslicer)    │
└─────────────┘    G-code + Meta     └──────────────────┘
```

1. User uploads an STL file to a part in LayerNexus.
2. User selects a slicer profile (machine + filament + print preset) and clicks **Slice**.
3. LayerNexus sends the STL file and profile configuration to the OrcaSlicer API.
4. OrcaSlicer API performs the slicing and returns the G-code file.
5. LayerNexus stores the G-code and displays metadata (estimated print time, filament usage).

---

## Configuration

### Environment Variable

| Variable | Description | Default |
|---|---|---|
| `ORCASLICER_API_URL` | URL of the OrcaSlicer API service | `http://localhost:3000` |

When using Docker Compose, this is automatically set to `http://orcaslicer:3000` in the default configuration.

### Docker Compose

The OrcaSlicer API container is included in the default `docker-compose.yml`:

```yaml
orcaslicer:
  image: ghcr.io/afkfelix/orca-slicer-api:latest-orca2.3.1
  ports:
    - "3000:3000"
  volumes:
    - orcaslicer_data:/app/data
  restart: unless-stopped
```

---

## Importing Profiles

Before slicing, you need to import OrcaSlicer profiles into LayerNexus. Three types of profiles are required:

### 1. Machine Profile

Defines the printer hardware characteristics:

- Build volume dimensions
- Nozzle diameter
- Heated bed capability
- Printer type (FDM)

### 2. Filament Profile

Defines filament-specific settings:

- Extrusion temperature
- Bed temperature
- Flow ratio
- Cooling settings
- Material type (PLA, PETG, ABS, etc.)

### 3. Print Preset

Defines print quality and speed settings:

- Layer height
- Print speed
- Infill pattern and density
- Support settings
- Wall count

### How to Import

1. Open **OrcaSlicer** desktop application.
2. Export your profiles as JSON files.
3. In LayerNexus, go to **OrcaSlicer Profiles**.
4. Click **Import** and upload the profile files.
5. The profiles are now available for slicing operations.

!!! tip
    You can set a default slicer profile on a project. All parts in that project will use the default profile unless overridden.

---

## Slicing Workflow

1. Navigate to a part that has an STL file uploaded.
2. Select the slicer profile combination (machine + filament + print preset).
3. Click **Slice**.
4. Wait for the OrcaSlicer API to process the file.
5. Once complete, the G-code is stored and slicing metadata is displayed:
    - Estimated print time
    - Filament usage (grams and meters)
    - Layer count

!!! warning "OrcaSlicer API Availability"
    If the OrcaSlicer API container is not running or not reachable, slicing will fail with an error message. Check that the `orcaslicer` service is running:

    ```bash
    docker compose ps orcaslicer
    ```

---

## Next Steps

- [Upload G-code to printers via Moonraker](moonraker.md)
- [Track filament with Spoolman](spoolman.md)
