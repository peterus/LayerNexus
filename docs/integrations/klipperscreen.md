# KlipperScreen Operator Dialogs

When a print started from the LayerNexus queue finishes, your printer's touchscreen
([KlipperScreen](https://github.com/KlipperScreen/KlipperScreen)) asks whether the plate
is **Good** or **Bad** — and then offers to start the next queued job. You never have to
walk to a PC to review a plate or press **Run Next**.

The same dialogs also appear in **Mainsail** (both render Klipper's macro prompts).

---

## How It Works

1. **The print ends** (completed, cancelled or error). The printer shows a dialog with the
   job name, `Plate n of m` and — if the print did not complete — how it ended.
   Buttons: **Good** / **Bad**.
2. **You press Good or Bad.** LayerNexus records the result exactly like Pass/Fail on the
   review page in the web UI (Bad requeues the plate until its retries are used up).
3. **Next job.** The follow-up dialog shows the result (e.g. `Plate passed (3/5 plates done)`)
   and the next waiting job for this printer, including the **filament it expects**
   (material and colour) and the estimated print time — so you can swap the spool first.
   Buttons: **Start** / **Later**. If the queue is empty, it says so.
4. **Start** starts exactly the job shown, with the same rules as **Run Next**: the printer
   must be free and the job must still be next in the queue. Otherwise you see
   `Already handled` and nothing happens. **Later** just closes the dialog.

!!! important "A print never starts by itself"
    Pressing **Good** never starts anything. A new print only starts when you press
    **Start**.

Prints you start yourself (not from the LayerNexus queue) never get a dialog.

If you review a plate in the web UI while its dialog is still open on the printer, the
dialog closes by itself within a few seconds.

### The LayerNexus menu entry

KlipperScreen gets a **LayerNexus** entry in its main menu. It re-opens the right dialog:
the pending review if there is one, otherwise the next job (or `Queue empty` /
`Printer busy`). Use it when you closed a dialog, after a Klipper restart, or whenever you
want to start the next job from the printer.

---

## Requirements

- The LayerNexus **worker** must be running (`moonraker_worker`, the `worker` service in
  Docker Compose). If it is down, no dialogs appear — the web UI keeps working as usual.
- Your `printer.cfg` must contain a `[respond]` section. Most setups (and Mainsail's
  default config) already have it; if not, add:

  ```ini
  [respond]
  ```

---

## Setup

### 1. Enable the dialogs for the printer

In LayerNexus go to **Printers → Edit** and tick
**Show operator dialogs on the printer**. The setting is per printer and off by default —
only enable it for printers that have the macros installed (step 2).

### 2. Install the printer files

LayerNexus ships two small files in the `klipper/` folder of its repository:

| File | Purpose |
|---|---|
| [`klipper/layernexus.cfg`](https://github.com/peterus/LayerNexus/blob/main/klipper/layernexus.cfg) | Macros `LN_REVIEW`, `LN_NEXT_JOB`, `LN_MENU`, `LN_CLOSE` used by the dialog buttons |
| [`klipper/layernexus_klipperscreen.conf`](https://github.com/peterus/LayerNexus/blob/main/klipper/layernexus_klipperscreen.conf) | The **LayerNexus** entry in the KlipperScreen main menu |

1. Upload both files to `~/printer_data/config/` (e.g. in Mainsail or Fluidd under
   **Machine → Config Files → Upload**).
2. Add this line to `printer.cfg`:

    ```ini
    [include layernexus.cfg]
    ```

3. Add this line to `KlipperScreen.conf`:

    ```ini
    [include layernexus_klipperscreen.conf]
    ```

4. Run `FIRMWARE_RESTART` (only while the printer is idle) and restart KlipperScreen.

LayerNexus never writes to your printer's configuration — these files are installed by you.

!!! note "Updates"
    The macros contain no logic; they only pass the button press back to LayerNexus. When the
    LayerNexus release notes say the macros changed, upload the new files again and run
    `FIRMWARE_RESTART`.

---

## Access

Anyone who can send G-code to the printer (touchscreen, Mainsail console) can review plates
and start the next queued job of that printer. Physical/printer access is treated as
operator access. Actions are logged by the worker as coming from the printer.

---

## Troubleshooting

**No dialog appears when a print ends**

- Check that **Show operator dialogs on the printer** is enabled for the printer.
- Check that the worker is running: `docker compose logs worker --tail=50`.
- Was the print started from the LayerNexus queue? Manual prints never get a dialog.
- Press **LayerNexus** in the KlipperScreen menu to show the dialog again.

**`Unknown command: "RESPOND"` or `"LN_REVIEW"` in the console / worker log**

- `[respond]` is missing in `printer.cfg`, or `layernexus.cfg` is not included.
  Fix the config and run `FIRMWARE_RESTART`.

**The LayerNexus menu entry is missing in KlipperScreen**

- Check the `[include layernexus_klipperscreen.conf]` line in `KlipperScreen.conf` and
  restart KlipperScreen.
