"""Tests for the Moonraker/Klipper rendering of operator dialogs."""

import re
from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase

from core.services.moonraker_operator_ui import (
    END_PROMPT_SCRIPT,
    action_to_gcode,
    parse_operator_response,
    render_prompt,
    sanitize_prompt_text,
)
from core.services.operator_dialog import DialogButton, Dismiss, OperatorDialog, Review, ShowMenu, StartNext

KLIPPER_DIR = Path(settings.BASE_DIR) / "klipper"

#: Characters that end Klipper's argument parsing (comments) or break the
#: quoted ``MSG="…"`` / the ``label|gcode|style`` button split.
GCODE_BREAKERS = set('"\\;#*|{}\n\r')


class ActionToGcodeTests(SimpleTestCase):
    def test_action_to_gcode_review_pass(self):
        self.assertEqual(action_to_gcode(Review(12, True)), "LN_REVIEW ENTRY=12 RESULT=PASS")

    def test_action_to_gcode_review_fail(self):
        self.assertEqual(action_to_gcode(Review(12, False)), "LN_REVIEW ENTRY=12 RESULT=FAIL")

    def test_action_to_gcode_start_next(self):
        self.assertEqual(action_to_gcode(StartNext(13)), "LN_NEXT_JOB ENTRY=13")

    def test_action_to_gcode_dismiss(self):
        self.assertEqual(action_to_gcode(Dismiss()), "LN_CLOSE")

    def test_action_to_gcode_show_menu(self):
        self.assertEqual(action_to_gcode(ShowMenu()), "LN_MENU")


class ParseOperatorResponseTests(SimpleTestCase):
    def test_parse_operator_response_roundtrip(self):
        self.assertEqual(parse_operator_response("LN REVIEW ENTRY=12 RESULT=PASS"), Review(12, True))
        self.assertEqual(parse_operator_response("LN REVIEW ENTRY=12 RESULT=FAIL"), Review(12, False))
        self.assertEqual(parse_operator_response("LN NEXT ENTRY=13"), StartNext(13))
        self.assertEqual(parse_operator_response("LN MENU"), ShowMenu())
        self.assertEqual(parse_operator_response("  LN MENU\n"), ShowMenu())

    def test_parse_ignores_prompt_and_noise(self):
        for line in (
            "// action:prompt_show",
            "// action:prompt_button Good|LN_REVIEW ENTRY=1 RESULT=PASS|primary",
            "echo: hi",
            "LN REVIEW ENTRY=x RESULT=PASS",
            "LN REVIEW ENTRY=1 RESULT=MAYBE",
            "LN REVIEW ENTRY=1",
            "LN NEXT ENTRY=",
            "LN MENU extra",
            "XLN MENU",
            "",
        ):
            with self.subTest(line=line):
                self.assertIsNone(parse_operator_response(line))

    def test_parse_non_string_returns_none(self):
        self.assertIsNone(parse_operator_response(None))  # type: ignore[arg-type]
        self.assertIsNone(parse_operator_response(42))  # type: ignore[arg-type]


class SanitizeTests(SimpleTestCase):
    def test_sanitize_strips_gcode_breakers(self):
        cleaned = sanitize_prompt_text('Bracket "v2"; #3 | x\ny {a} *b* \\c')
        self.assertFalse(GCODE_BREAKERS & set(cleaned), cleaned)
        self.assertEqual(cleaned, "Bracket v2 3 x y a b c")

    def test_sanitize_umlauts(self):
        self.assertEqual(sanitize_prompt_text("Gehäuse für Größe"), "Gehause fur Groe")

    def test_sanitize_truncates(self):
        cleaned = sanitize_prompt_text("x" * 200, max_len=20)
        self.assertEqual(len(cleaned), 20)
        self.assertTrue(cleaned.endswith("..."))

    def test_sanitize_keeps_short_text(self):
        self.assertEqual(sanitize_prompt_text("Plate 1 of 2"), "Plate 1 of 2")


class RenderPromptTests(SimpleTestCase):
    def _dialog(self, *lines: str) -> OperatorDialog:
        return OperatorDialog(
            title="LayerNexus",
            lines=lines,
            buttons=(
                DialogButton("Good", Review(5, True), "primary"),
                DialogButton("Bad", Review(5, False), "error"),
            ),
        )

    def test_render_prompt_structure(self):
        script = render_prompt(self._dialog("Bracket", "Plate 1 of 2"))
        lines = script.split("\n")
        self.assertEqual(lines[0], END_PROMPT_SCRIPT)
        self.assertEqual(lines[1], 'RESPOND TYPE=command MSG="action:prompt_begin LayerNexus"')
        self.assertEqual(lines[-1], 'RESPOND TYPE=command MSG="action:prompt_show"')
        self.assertIn('RESPOND TYPE=command MSG="action:prompt_text Plate 1 of 2"', lines)
        self.assertIn(
            'RESPOND TYPE=command MSG="action:prompt_button Good|LN_REVIEW ENTRY=5 RESULT=PASS|primary"',
            lines,
        )
        start = lines.index('RESPOND TYPE=command MSG="action:prompt_button_group_start"')
        end = lines.index('RESPOND TYPE=command MSG="action:prompt_button_group_end"')
        self.assertLess(start, end)
        for line in lines:
            self.assertRegex(line, r'^RESPOND TYPE=command MSG="action:prompt_[a-z_]+( [^"]*)?"$')

    def test_render_prompt_sanitizes_untrusted_text(self):
        script = render_prompt(self._dialog('Evil "job"; #x | y\nFIRMWARE_RESTART'))
        lines = script.split("\n")
        self.assertIn('RESPOND TYPE=command MSG="action:prompt_text Evil job x y FIRMWARE_RESTART"', lines)
        for line in lines:
            msg = re.fullmatch(r'RESPOND TYPE=command MSG="(.*)"', line).group(1)
            self.assertFalse(set('";#*\\') & set(msg), line)

    def test_render_prompt_rejects_unknown_style(self):
        dialog = OperatorDialog("T", (), (DialogButton("X", Dismiss(), "bogus"),))
        self.assertIn("action:prompt_button X|LN_CLOSE|primary", render_prompt(dialog))


class KlipperFilesTests(SimpleTestCase):
    def test_klipper_cfg_defines_macros_matching_parser(self):
        cfg = (KLIPPER_DIR / "layernexus.cfg").read_text()
        for macro in ("LN_REVIEW", "LN_NEXT_JOB", "LN_MENU", "LN_CLOSE"):
            self.assertIn(f"[gcode_macro {macro}]", cfg)
        self.assertIn("PREFIX=LN", cfg)
        self.assertIn('MSG="REVIEW ENTRY=', cfg)
        self.assertIn('MSG="NEXT ENTRY=', cfg)
        self.assertIn('MSG="MENU"', cfg)
        # [respond] is a documented requirement, not part of the file (duplicate sections break Klipper).
        self.assertNotRegex(cfg, r"(?m)^\[respond\]")

    def test_klipperscreen_conf_calls_ln_menu(self):
        conf = (KLIPPER_DIR / "layernexus_klipperscreen.conf").read_text()
        self.assertIn("[menu __main layernexus]", conf)
        self.assertIn("method: printer.gcode.script", conf)
        self.assertIn('"script":"LN_MENU"', conf.replace(" ", ""))
