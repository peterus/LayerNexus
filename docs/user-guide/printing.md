# Print Jobs & Queue

LayerNexus provides print job tracking and a priority-based queue system for managing prints across multiple printers.

## Creating Print Jobs

Print jobs are created from parts that have been sliced (G-code available).

1. Open a part detail page.
2. Click **Create Print Job** (or **Slice** first if no G-code exists).
3. Select the target printer.
4. Review the job details (estimated print time, filament usage).
5. Click **Create**.

---

## Print Job Lifecycle

Each print job progresses through the following statuses:

| Status | Description |
|---|---|
| **Pending** | Job created, waiting to be queued or started |
| **Queued** | Job is in the print queue, waiting for a printer |
| **Printing** | Job is currently printing on a printer |
| **Completed** | Print finished successfully |
| **Failed** | Print failed or was cancelled |

---

## Print Queue

The print queue provides priority-based management across multiple printers.

### Adding Jobs to the Queue

1. Go to **Print Queue** in the navigation bar.
2. Click **Add to Queue**.
3. Select the print job and target printer.
4. Set the priority level.
5. Click **Add**.

### Queue Priority

Jobs in the queue are ordered by priority. Higher-priority jobs are printed first when a printer becomes available.

### Managing the Queue

| Action | Required Role |
|---|---|
| View queue | Any authenticated user |
| Add to queue | Admin, Operator, Designer |
| Remove from queue | Admin, Operator, Designer |

!!! note
    Both Operators and Designers can manage the print queue. Only Operators and Admins can actually start prints on printers (upload G-code and trigger printing).

---

## Uploading G-code to Printers

Once a job is ready:

1. Open the print job detail page.
2. Click **Upload to Printer**.
3. The G-code file is sent to the printer using the appropriate backend:

    === "Klipper / Moonraker"

        The file is uploaded via the [Moonraker API](../integrations/moonraker.md). The printer must be online and reachable.

    === "Bambu Lab"

        The file is uploaded via **LAN FTP** (if a LAN IP is configured) or the **Bambu Lab Cloud**. See [Bambu Lab](../integrations/bambulab.md) for details.

4. Click **Start Print** to begin.

!!! warning "Printer Connectivity"
    The printer must be online and reachable. For Klipper, check the Moonraker URL in the printer profile. For Bambu Lab, ensure the Cloud token hasn't expired (check **Bambu Lab Accounts**).

---

## Cost Calculation

Each print job tracks costs based on the printer's cost profile:

| Cost Component | Description |
|---|---|
| **Filament** | Material cost based on weight used and spool pricing |
| **Electricity** | Energy cost based on print time and power consumption |
| **Depreciation** | Printer depreciation based on print time |
| **Maintenance** | Maintenance cost based on print time |

### Viewing Costs

- **Per job:** View the cost breakdown on the print job detail page.
- **Per project:** View aggregated costs on the project detail page, including all sub-projects.

---

## Workflow Summary

```
STL Upload → Slice (OrcaSlicer API) → G-code → Add to Queue → Upload to Printer → Print → Complete
```

1. **Upload** STL file to a part
2. **Slice** using OrcaSlicer profiles
3. **Create** a print job
4. **Queue** the job with priority
5. **Upload** G-code to the printer (via Moonraker, LAN FTP, or Bambu Lab Cloud)
6. **Start** the print
7. **Track** status until completion

---

## Next Steps

- [Project management and sub-projects](projects.md)
- [User roles and permissions](roles.md)
- [OrcaSlicer integration](../integrations/orcaslicer.md)
- [Klipper / Moonraker integration](../integrations/moonraker.md)
- [Bambu Lab integration](../integrations/bambulab.md)