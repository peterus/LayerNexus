# Klipper / Moonraker

LayerNexus connects to your **Klipper-based** 3D printers through [Moonraker](https://github.com/Arksine/moonraker) — the API that runs alongside Klipper on your printer. This lets you upload G-code, start prints, and track progress right from LayerNexus.

!!! tip "Using Bambu Lab printers?"
    LayerNexus also supports Bambu Lab printers via the Cloud API. See [Bambu Lab](bambulab.md) for setup instructions.

---

## Adding a Printer

1. Go to **Printers** in the navigation bar.
2. Click **Add Printer**.
3. Select **Klipper/Moonraker** as the printer type.
4. Fill in:

| Field | What to Enter | Example |
|---|---|---|
| **Name** | A friendly name for the printer | `Voron 2.4 #1` |
| **Moonraker URL** | The URL where Moonraker is running | `http://192.168.1.100:7125` |
| **API Key** | Only needed if your Moonraker requires authentication | _(leave empty if not needed)_ |

5. Click **Save**.

### Finding Your Moonraker URL

Moonraker usually runs on port `7125` on the same device as Klipper. You can test if it's reachable by opening this in your browser:

```
http://<your-printer-ip>:7125/server/info
```

If you see a JSON response, it's working.

!!! important "Network Access"
    LayerNexus needs to be able to reach your printer's IP address. If LayerNexus runs in Docker, make sure the container can access your local network. On most setups this works out of the box.

---

## Printing Workflow

Once you have a printer set up and a part sliced (see [OrcaSlicer](orcaslicer.md)):

1. Open the part or print job detail page.
2. Click **Upload to Printer** — the G-code file is sent to your printer.
3. Click **Start Print** — the printer starts printing.

You can also **cancel** a running print from LayerNexus.

---

## Print Status

LayerNexus tracks each print job through its lifecycle:

| Status | Meaning |
|---|---|
| **Pending** | Job created, not yet started |
| **Printing** | Currently printing |
| **Completed** | Finished successfully |
| **Failed** | Print failed or was cancelled |

---

## Troubleshooting

**Can't connect to the printer?**

- Double-check the Moonraker URL in the printer profile.
- Test connectivity: open `http://<printer-ip>:7125/server/info` in your browser.
- Make sure no firewall is blocking port `7125`.
- If LayerNexus runs in Docker, verify the container can reach your local network.

**Upload fails?**

- Check that Moonraker is running on the printer.
- Verify the API key if authentication is enabled.

---

## Next Steps

- [Set up Bambu Lab printers](bambulab.md)
- [Track filament with Spoolman](spoolman.md)
- [Manage the print queue](../user-guide/printing.md)
