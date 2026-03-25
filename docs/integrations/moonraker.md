# Klipper / Moonraker

LayerNexus integrates with [Moonraker](https://github.com/Arksine/moonraker) — the API server for [Klipper](https://www.klipper3d.org/) — to upload G-code files and control 3D printers directly from the web interface.

## What is Moonraker?

Moonraker is a web API that sits alongside Klipper on your 3D printer. It exposes a REST API and WebSocket interface for:

- Uploading G-code files
- Starting, pausing, and canceling prints
- Monitoring printer status (temperatures, position, progress)
- Managing files on the printer

LayerNexus communicates with Moonraker to provide seamless printer control from the project management interface.

---

## Setting Up Printer Profiles

To connect a printer to LayerNexus:

1. Go to **Printers** in the navigation bar.
2. Click **Add Printer**.
3. Fill in the printer details:

| Field | Description | Example |
|---|---|---|
| **Name** | A friendly name for the printer | `Voron 2.4 #1` |
| **Moonraker URL** | The URL where Moonraker is accessible | `http://192.168.1.100:7125` |
| **API Key** | Moonraker API key (if authentication is enabled) | _(optional)_ |

4. Click **Save**.

!!! important "Network Connectivity"
    LayerNexus (or the Docker container running it) must be able to reach the Moonraker URL over your network. If running in Docker, ensure the container can access your local network. You may need to use `host` networking or configure Docker network settings.

---

## Moonraker URL and API Key

### Finding Your Moonraker URL

Moonraker typically runs on port `7125` on the same host as Klipper:

```
http://<printer-ip>:7125
```

You can verify connectivity by visiting `http://<printer-ip>:7125/server/info` in your browser — it should return a JSON response with server information.

### API Key Authentication

If your Moonraker instance requires authentication:

1. Find the API key in Moonraker's configuration or web interface (e.g., Mainsail or Fluidd settings).
2. Enter it in the **API Key** field when creating the printer profile in LayerNexus.

!!! tip
    For printers on a trusted local network, you can configure Moonraker to allow unauthenticated access from specific IP ranges using its `[authorization]` configuration section.

---

## G-code Upload

After slicing a part (see [OrcaSlicer Integration](orcaslicer.md)), you can upload the resulting G-code to a printer:

1. Open the part detail page with a sliced G-code file.
2. Click **Upload to Printer**.
3. Select the target printer from the dropdown.
4. The G-code file is uploaded to Moonraker's file storage.

---

## Print Control

Once G-code is uploaded to a printer, you can:

- **Start Print** — Begin printing the uploaded file
- **Cancel Print** — Cancel a running print job

Print status is tracked in LayerNexus and updated from Moonraker.

---

## Status Monitoring

LayerNexus monitors print status from Moonraker, tracking jobs through their lifecycle:

| Status | Description |
|---|---|
| **Pending** | Job created, not yet started |
| **Printing** | Currently printing |
| **Completed** | Print finished successfully |
| **Failed** | Print failed or was cancelled |

---

## Troubleshooting

### Cannot Connect to Moonraker

- Verify the Moonraker URL is correct and accessible from the LayerNexus server/container.
- Check that Moonraker is running: `curl http://<printer-ip>:7125/server/info`
- If running LayerNexus in Docker, ensure the container can reach the printer's IP address.

### Upload Fails

- Check that Moonraker's file upload endpoint is accessible.
- Verify the API key (if authentication is enabled).
- Check Moonraker logs for error details.

### Connection Timeout

- Ensure no firewall is blocking port `7125` between LayerNexus and the printer.
- For Docker, verify network configuration allows outbound connections to your local network.

---

## Next Steps

- [Track filament with Spoolman](spoolman.md)
- [Manage print queue](../user-guide/printing.md)
