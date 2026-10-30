"""Show operator dialogs on Klipper printers (KlipperScreen, Mainsail).

Renders :class:`~core.services.operator_dialog.OperatorDialog` objects as
Klipper macro prompts (``RESPOND TYPE=command MSG="action:prompt_…"``) and
turns the echo lines of the LayerNexus macros (``klipper/layernexus.cfg``)
back into abstract operator actions.

Wire format (the macros and :func:`parse_operator_response` must agree):

* Buttons run ``LN_REVIEW ENTRY=<pk> RESULT=PASS|FAIL``,
  ``LN_NEXT_JOB ENTRY=<pk>``, ``LN_MENU`` or ``LN_CLOSE``.
* The macros close the prompt and echo ``LN REVIEW ENTRY=<pk> RESULT=PASS``,
  ``LN NEXT ENTRY=<pk>`` or ``LN MENU`` (``RESPOND PREFIX=LN``), which
  Moonraker broadcasts as ``notify_gcode_response``.

This module is Moonraker/Klipper specific and must only be imported by the
``moonraker_worker`` management command.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from typing import Any

from asgiref.sync import sync_to_async
from django.db import connection

from core.models import PrinterProfile, PrintQueue
from core.services.moonraker import MoonrakerClient, MoonrakerError
from core.services.operator_dialog import (
    DIALOG_TITLE,
    DialogButton,
    Dismiss,
    OperatorAction,
    OperatorDialog,
    Review,
    ShowMenu,
    StartNext,
    handle_action,
    review_dialog,
)

logger = logging.getLogger(__name__)

__all__ = [
    "END_PROMPT_SCRIPT",
    "RESPONSE_PREFIX",
    "MoonrakerOperatorUI",
    "action_to_gcode",
    "parse_operator_response",
    "render_prompt",
    "sanitize_prompt_text",
]

#: Prefix of the echo lines written by the LayerNexus macros.
RESPONSE_PREFIX = "LN"

#: Script that closes an open prompt.
END_PROMPT_SCRIPT = 'RESPOND TYPE=command MSG="action:prompt_end"'

#: Button styles supported by KlipperScreen and Mainsail.
BUTTON_STYLES = ("primary", "secondary", "info", "warning", "error")

#: Characters removed from prompt text.  ``"`` and ``\\`` break the quoted
#: ``MSG="…"`` argument, ``;``, ``#`` and ``*`` start a comment / checksum in
#: Klipper's command parser (even inside quotes), ``|`` is the button field
#: separator and ``{``/``}`` are template delimiters.
_FORBIDDEN_CHARS = re.compile(r'["\\;#*|{}]')
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WHITESPACE = re.compile(r"\s+")

#: Shown when handling a button press raised unexpectedly.
ACTION_FAILED_DIALOG = OperatorDialog(
    title=DIALOG_TITLE,
    lines=("Action failed", "See the LayerNexus worker log for details."),
    buttons=(DialogButton("Close", Dismiss(), "secondary"),),
)

_RESPONSE_RE = re.compile(
    rf"^{RESPONSE_PREFIX} (?:"
    r"REVIEW ENTRY=(?P<review_pk>\d+) RESULT=(?P<result>PASS|FAIL)"
    r"|NEXT ENTRY=(?P<next_pk>\d+)"
    r"|(?P<menu>MENU)"
    r")$"
)


def sanitize_prompt_text(text: str, max_len: int = 80) -> str:
    """Make arbitrary text safe for a ``RESPOND MSG="action:prompt_…"`` line.

    Transliterates to ASCII (``"ä"`` → ``"a"``), drops characters that would
    break the command or the button field split, turns control characters
    (newlines) into spaces, collapses whitespace and truncates.

    Args:
        text: The untrusted text (e.g. a job name).
        max_len: Maximum length of the result, including a trailing ``"..."``.

    Returns:
        The sanitised text.
    """
    decomposed = unicodedata.normalize("NFKD", str(text))
    # Drop accents ("a" + combining diaeresis -> "a"); other non-ASCII characters
    # (CJK, Cyrillic, "ß", …) become "?" so a name never sanitises to nothing.
    without_marks = "".join(char for char in decomposed if not unicodedata.combining(char))
    ascii_text = without_marks.encode("ascii", "replace").decode("ascii")
    ascii_text = _CONTROL_CHARS.sub(" ", ascii_text)
    ascii_text = _FORBIDDEN_CHARS.sub("", ascii_text)
    ascii_text = _WHITESPACE.sub(" ", ascii_text).strip()
    if len(ascii_text) > max_len:
        ascii_text = f"{ascii_text[: max_len - 3].rstrip()}..."
    return ascii_text


def action_to_gcode(action: OperatorAction) -> str:
    """Return the macro call a button runs for ``action``.

    Args:
        action: The abstract operator action.

    Returns:
        The G-code command, e.g. ``"LN_REVIEW ENTRY=12 RESULT=PASS"``.
    """
    if isinstance(action, Review):
        result = "PASS" if action.passed else "FAIL"
        return f"LN_REVIEW ENTRY={int(action.entry_pk)} RESULT={result}"
    if isinstance(action, StartNext):
        return f"LN_NEXT_JOB ENTRY={int(action.entry_pk)}"
    if isinstance(action, ShowMenu):
        return "LN_MENU"
    return "LN_CLOSE"


def _prompt_command(command: str, argument: str = "") -> str:
    message = f"action:prompt_{command}"
    if argument:
        message = f"{message} {argument}"
    return f'RESPOND TYPE=command MSG="{message}"'


def render_prompt(dialog: OperatorDialog) -> str:
    """Render a dialog as a newline-separated Klipper prompt script.

    Args:
        dialog: The dialog.

    Returns:
        The G-code script (closes any open prompt first, ends with ``prompt_show``).
    """
    commands = [END_PROMPT_SCRIPT, _prompt_command("begin", sanitize_prompt_text(dialog.title, 40))]
    for line in dialog.lines:
        text = sanitize_prompt_text(line)
        if text:
            commands.append(_prompt_command("text", text))
    if dialog.buttons:
        commands.append(_prompt_command("button_group_start"))
        for button in dialog.buttons:
            style = button.style if button.style in BUTTON_STYLES else "primary"
            label = sanitize_prompt_text(button.label, 30)
            commands.append(_prompt_command("button", f"{label}|{action_to_gcode(button.action)}|{style}"))
        commands.append(_prompt_command("button_group_end"))
    commands.append(_prompt_command("show"))
    return "\n".join(commands)


def parse_operator_response(line: Any) -> OperatorAction | None:
    """Parse a G-code response line into an operator action.

    Args:
        line: One line of a ``notify_gcode_response`` notification.

    Returns:
        The action for a LayerNexus macro echo, else ``None``.
    """
    if not isinstance(line, str):
        return None
    match = _RESPONSE_RE.match(line.strip())
    if match is None:
        return None
    if match["review_pk"] is not None:
        return Review(int(match["review_pk"]), match["result"] == "PASS")
    if match["next_pk"] is not None:
        return StartNext(int(match["next_pk"]))
    return ShowMenu()


def _handle_action_in_thread(printer: PrinterProfile, action: OperatorAction) -> OperatorDialog | None:
    """Run :func:`handle_action` in a worker thread and release its DB connection."""
    try:
        return handle_action(printer, action)
    finally:
        connection.close()


@sync_to_async
def _load_review_dialog(entry_pk: int, printer_pk: int) -> OperatorDialog | None:
    entry = (
        PrintQueue.objects.awaiting_review()
        .filter(pk=entry_pk, printer_id=printer_pk)
        .select_related("plate__print_job")
        .first()
    )
    if entry is None or entry.plate is None:
        return None
    return review_dialog(entry)


@sync_to_async
def _is_awaiting_review(entry_pk: int) -> bool:
    return PrintQueue.objects.awaiting_review().filter(pk=entry_pk).exists()


class MoonrakerOperatorUI:
    """Operator dialogs for one Klipper printer, driven by the worker.

    Sends prompts via Moonraker's REST API and handles the LayerNexus macro
    echoes the worker receives on its WebSocket.  Button presses are handled
    one at a time per printer (double presses serialise and then see
    "Already handled").
    """

    def __init__(self, printer: PrinterProfile, client: MoonrakerClient) -> None:
        """Initialise the UI.

        Args:
            printer: The printer (must have ``operator_prompts_enabled``).
            client: Moonraker REST client for this printer.
        """
        self.printer = printer
        self.client = client
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        #: Queue entry of the review dialog currently shown, if any.
        self.open_review_pk: int | None = None

    async def show(self, dialog: OperatorDialog | None) -> None:
        """Show ``dialog`` on the printer (``None`` closes the open prompt).

        Send errors (printer offline, macros/``[respond]`` missing) are logged
        and swallowed so they never disturb status sync.

        Args:
            dialog: The dialog to show, or ``None`` to close.
        """
        script = END_PROMPT_SCRIPT if dialog is None else render_prompt(dialog)
        self.open_review_pk = _review_entry_pk(dialog)
        try:
            await sync_to_async(self.client.run_gcode, thread_sensitive=False)(script)
        except MoonrakerError as exc:
            logger.warning("Could not show operator dialog on printer %s: %s", self.printer.name, exc)

    async def on_gcode_response(self, params: list[Any]) -> bool:
        """Handle a ``notify_gcode_response`` notification.

        Actions are processed in a background task so a slow G-code upload
        does not stall the printer's event stream.

        Args:
            params: The notification's ``params`` (list of response lines).

        Returns:
            ``True`` if at least one LayerNexus echo line was found.
        """
        handled = False
        for line in params or []:
            action = parse_operator_response(line)
            if action is None:
                continue
            handled = True
            task = asyncio.create_task(self._run_action(action), name=f"operator_action_{self.printer.pk}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return handled

    async def _run_action(self, action: OperatorAction) -> None:
        async with self._lock:
            try:
                dialog = await sync_to_async(_handle_action_in_thread, thread_sensitive=False)(self.printer, action)
            except Exception:  # noqa: BLE001 - one failing action must not kill the printer task
                logger.exception("Operator action %r from printer %s failed", action, self.printer.name)
                # The macro already closed the prompt: never fail silently on the printer.
                await self.show(ACTION_FAILED_DIALOG)
                return
            if dialog is None:
                # The macro already closed the prompt on the printer.
                self.open_review_pk = None
                return
            await self.show(dialog)

    async def wait_idle(self) -> None:
        """Wait until all pending button actions are processed (for tests/shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def cancel_pending(self) -> None:
        """Cancel pending button actions (printer task is shutting down)."""
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def on_review_pending(self, entry_pk: int) -> None:
        """Show the Good/Bad dialog for an entry that just finished printing.

        Args:
            entry_pk: The queue entry now awaiting review.
        """
        async with self._lock:
            dialog = await _load_review_dialog(entry_pk, self.printer.pk)
            if dialog is not None:
                await self.show(dialog)

    async def close_if_stale(self) -> None:
        """Close the open review dialog if its entry was reviewed elsewhere (e.g. on the web)."""
        if self._lock.locked():
            return
        async with self._lock:
            entry_pk = self.open_review_pk
            if entry_pk is None or await _is_awaiting_review(entry_pk):
                return
            logger.info("Closing stale review dialog for queue entry %s on printer %s", entry_pk, self.printer.name)
            await self.show(None)


def _review_entry_pk(dialog: OperatorDialog | None) -> int | None:
    """Return the queue entry a review dialog asks about, else ``None``."""
    if dialog is None:
        return None
    for button in dialog.buttons:
        if isinstance(button.action, Review):
            return button.action.entry_pk
    return None
