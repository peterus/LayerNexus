from django.conf import settings
from django.test import SimpleTestCase


class SessionLifetimeSettingsTests(SimpleTestCase):
    """Guards the configurable session-lifetime defaults."""

    def test_default_session_age_is_14_days(self) -> None:
        self.assertEqual(settings.SESSION_COOKIE_AGE, 1209600)

    def test_sessions_are_sliding_by_default(self) -> None:
        self.assertTrue(settings.SESSION_SAVE_EVERY_REQUEST)
