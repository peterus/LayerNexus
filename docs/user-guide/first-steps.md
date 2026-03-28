# First Steps

This guide walks you through the essential tasks after you've got LayerNexus running (see [Quick Start](../quick-start.md)).

## 1. Create Your First Project

1. Log in with the account you created during setup.
2. Click **Projects** in the navigation bar.
3. Click **New Project**.
4. Fill in the project name and an optional description.
5. Optionally add a cover image — you can upload a file or paste from the clipboard.
6. Click **Save**.

!!! tip
    Projects can contain sub-projects with quantity multipliers. For example, a "Quadcopter" project might have a sub-project "Motor Mount" with quantity 4. See [Projects & Sub-Projects](projects.md) for more.

---

## 2. Upload STL Files

1. Open your project and navigate to the **Parts** section.
2. Click **Add Part**.
3. Enter the part name, quantity, color, and material.
4. Upload the STL file.
5. Click **Save**.

After uploading, you can preview the 3D model directly in the browser using the built-in viewer.

---

## 3. Set Up Printer Profiles

Before you can send G-code to a printer, create a printer profile. LayerNexus supports two types of printers:

=== "Klipper / Moonraker"

    1. Go to **Printers** in the navigation bar.
    2. Click **Add Printer**.
    3. Select **Klipper/Moonraker** as the printer type.
    4. Enter a name for the printer (e.g., "Voron 2.4").
    5. Enter the **Moonraker URL** — this is the URL where Moonraker is running on your printer (e.g., `http://192.168.1.100:7125`).
    6. Optionally add an **API key** if your Moonraker instance requires authentication.
    7. Click **Save**.

    !!! note
        LayerNexus connects to your printers via the [Moonraker API](../integrations/moonraker.md). Make sure LayerNexus can reach the Moonraker URL over your network.

=== "Bambu Lab"

    1. Go to **Bambu Lab Accounts** in the navigation bar.
    2. Click **Connect Account**.
    3. Log in with your Bambu Lab Cloud email and password.
    4. Enter the **6-digit verification code** sent to your email.
    5. Select your printer from the list of registered devices.
    6. Optionally enter the printer's **LAN IP address** for faster G-code uploads.
    7. Click **Connect Printer**.

    The wizard creates a printer profile automatically. See [Bambu Lab](../integrations/bambulab.md) for the full guide.

### Cost Profiles

You can attach a **cost profile** to a printer to track printing costs:

- Electricity cost per kWh
- Printer depreciation rate
- Maintenance cost per hour

These values are used to calculate per-job and per-project costs.

---

## 4. Import OrcaSlicer Profiles

LayerNexus uses the [OrcaSlicer API](../integrations/orcaslicer.md) for slicing. To slice parts, you need to import profiles:

1. Go to **OrcaSlicer Profiles** in the navigation bar.
2. Click **Import Profile**.
3. Upload three profile files exported from OrcaSlicer:
    - **Machine profile** (`.json`) — your printer hardware settings
    - **Filament profile** (`.json`) — filament settings (temperature, flow, etc.)
    - **Print preset** (`.json`) — print quality settings (layer height, speed, etc.)
4. Click **Save**.

!!! tip "Exporting Profiles from OrcaSlicer"
    In OrcaSlicer desktop, go to the profile editor and export each profile as a JSON file. These are the files you import into LayerNexus.

---

## 5. Your First Print Job

Once you have a project with parts, a printer profile, and slicer profiles set up:

1. Open a part detail page.
2. Click **Slice** to send the STL to OrcaSlicer for slicing.
3. Review the slicing results (estimated print time, filament usage).
4. Click **Upload to Printer** to send the G-code to your printer.
5. Click **Start Print** to begin printing.

### Print Queue

For managing multiple jobs across multiple printers:

1. Go to **Print Queue** in the navigation bar.
2. Add jobs to the queue with priority levels.
3. Assign jobs to available printers.
4. Track job status from pending through completion.

---

## Next Steps

- [Learn about roles and permissions](roles.md)
- [Manage projects and sub-projects](projects.md)
- [Set up Spoolman for filament tracking](../integrations/spoolman.md)
- [Set up HTTPS for remote access](../advanced/reverse-proxy.md)
