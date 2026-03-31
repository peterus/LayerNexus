"""Print queue service functions.

Provides reusable logic for starting prints via Moonraker, extracted
from the queue views so it can be called from both ``RunNextQueueView``
and ``RunAllQueuesView`` without duplication.
"""

import logging
import re

from django.utils import timezone

from core.models import PrintJob, PrintJobPlate, PrintQueue
from core.services.moonraker import MoonrakerClient, MoonrakerError

logger = logging.getLogger(__name__)

__all__ = [
    "start_print_for_queue_entry",
    "PrintStartError",
]


class PrintStartError(Exception):
    """Raised when a print could not be started."""


def start_print_for_queue_entry(entry: PrintQueue) -> str:
    """Upload G-code and start a print for the given queue entry.

    The entry must already be validated (waiting status, printer free,
    G-code file present).  This function handles:

    1. Building a filesystem-safe remote filename.
    2. Uploading the G-code to Moonraker.
    3. Starting the print via Moonraker.
    4. Updating the queue entry, plate, and job status/timestamps.

    Args:
        entry: A ``PrintQueue`` instance with ``plate__print_job`` and
            ``printer`` already selected/loaded.

    Returns:
        The remote filename used for the print.

    Raises:
        PrintStartError: If the entry has no G-code file.
        MoonrakerError: If the Moonraker API call fails.
    """
    plate = entry.plate
    job = plate.print_job
    printer = entry.printer

    gcode_file = plate.gcode_file
    if not gcode_file:
        raise PrintStartError(
            f"No G-code file for plate {plate.plate_number}. Slice the job first."
        )

    # Build a descriptive, filesystem-safe filename for Klipper
    safe_name = re.sub(r"[^\w\-]", "_", str(job))[:50]
    remote_filename = f"LN_{safe_name}_p{plate.plate_number}.gcode"

    client = MoonrakerClient(printer.moonraker_url, printer.moonraker_api_key)

    # Upload gcode and start the print
    client.upload_gcode(gcode_file.path, filename=remote_filename)
    client.start_print(remote_filename)

    # Update queue entry
    entry.status = PrintQueue.STATUS_PRINTING
    entry.started_at = timezone.now()
    entry.save(update_fields=["status", "started_at"])

    # Update plate status
    plate.status = PrintJobPlate.STATUS_PRINTING
    plate.klipper_job_id = remote_filename
    plate.started_at = timezone.now()
    plate.save(update_fields=["status", "klipper_job_id", "started_at"])

    # Update job status
    job.status = PrintJob.STATUS_PRINTING
    if not job.started_at:
        job.started_at = timezone.now()
    job.save(update_fields=["status", "started_at"])

    return remote_filename
