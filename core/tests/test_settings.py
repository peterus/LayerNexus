import importlib
import os
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase

from layernexus import settings as settings_module


class SessionLifetimeSettingsTests(SimpleTestCase):
    """Guards the configurable session-lifetime defaults and env overrides."""

    def test_default_session_age_is_14_days(self) -> None:
        self.assertEqual(settings.SESSION_COOKIE_AGE, 1209600)

    def test_sessions_are_sliding_by_default(self) -> None:
        self.assertTrue(settings.SESSION_SAVE_EVERY_REQUEST)

    def test_session_age_honours_environment_override(self) -> None:
        """A custom SESSION_COOKIE_AGE is parsed as an int, not left at the default."""
        try:
            with mock.patch.dict(os.environ, {"SESSION_COOKIE_AGE": "3600"}):
                reloaded = importlib.reload(settings_module)
                self.assertEqual(reloaded.SESSION_COOKIE_AGE, 3600)
        finally:
            # Restore the module *after* the patched environment is torn down,
            # otherwise the reload would re-read the override and leave the
            # shared settings module polluted for later tests.
            importlib.reload(settings_module)

    def test_sliding_sessions_can_be_disabled(self) -> None:
        """SESSION_SAVE_EVERY_REQUEST=0 disables the sliding window."""
        try:
            with mock.patch.dict(os.environ, {"SESSION_SAVE_EVERY_REQUEST": "0"}):
                reloaded = importlib.reload(settings_module)
                self.assertFalse(reloaded.SESSION_SAVE_EVERY_REQUEST)
        finally:
            importlib.reload(settings_module)

    def test_sliding_sessions_enabled_for_non_zero_flag(self) -> None:
        """Any value other than the literal "0" keeps the sliding window on."""
        try:
            with mock.patch.dict(os.environ, {"SESSION_SAVE_EVERY_REQUEST": "1"}):
                reloaded = importlib.reload(settings_module)
                self.assertTrue(reloaded.SESSION_SAVE_EVERY_REQUEST)
        finally:
            importlib.reload(settings_module)
