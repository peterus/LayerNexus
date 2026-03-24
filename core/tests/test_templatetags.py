"""Tests for custom template tags and filters."""

from django.test import TestCase


class DurationFormatFilterTests(TestCase):
    """Tests for the duration_format template filter."""

    def setUp(self):
        from core.templatetags.core_tags import duration_format

        self.f = duration_format

    def test_hours_and_minutes(self):
        from datetime import timedelta

        self.assertEqual(self.f(timedelta(hours=2, minutes=15)), "2h 15m")

    def test_only_minutes(self):
        from datetime import timedelta

        self.assertEqual(self.f(timedelta(minutes=45)), "45m")

    def test_only_hours(self):
        from datetime import timedelta

        self.assertEqual(self.f(timedelta(hours=3)), "3h")

    def test_only_seconds(self):
        from datetime import timedelta

        self.assertEqual(self.f(timedelta(seconds=30)), "30s")

    def test_zero(self):
        from datetime import timedelta

        self.assertEqual(self.f(timedelta(seconds=0)), "0s")

    def test_non_timedelta_passthrough(self):
        self.assertEqual(self.f("not a timedelta"), "not a timedelta")

    def test_negative_returns_zero(self):
        from datetime import timedelta

        self.assertEqual(self.f(timedelta(seconds=-10)), "0m")


class FileSizeFilterTests(TestCase):
    """Tests for the file_size template filter."""

    def setUp(self):
        from core.templatetags.core_tags import file_size

        self.f = file_size

    def test_bytes(self):
        self.assertEqual(self.f(512), "512.0 B")

    def test_kilobytes(self):
        self.assertEqual(self.f(2048), "2.0 KB")

    def test_megabytes(self):
        self.assertEqual(self.f(5 * 1024 * 1024), "5.0 MB")

    def test_gigabytes(self):
        self.assertEqual(self.f(2 * 1024**3), "2.0 GB")

    def test_non_numeric_passthrough(self):
        self.assertEqual(self.f("abc"), "abc")

    def test_none_passthrough(self):
        self.assertIsNone(self.f(None))


class PercentageFilterTests(TestCase):
    """Tests for the percentage template filter."""

    def setUp(self):
        from core.templatetags.core_tags import percentage

        self.f = percentage

    def test_half(self):
        self.assertEqual(self.f(1, 2), 50)

    def test_full(self):
        self.assertEqual(self.f(10, 10), 100)

    def test_zero_total(self):
        self.assertEqual(self.f(5, 0), 0)

    def test_none_value(self):
        self.assertEqual(self.f(None, 10), 0)


class GramsToKgFilterTests(TestCase):
    """Tests for the grams_to_kg template filter."""

    def setUp(self):
        from core.templatetags.core_tags import grams_to_kg

        self.f = grams_to_kg

    def test_conversion(self):
        self.assertEqual(self.f(1500), "1.50 kg")

    def test_zero(self):
        self.assertEqual(self.f(0), "0.00 kg")

    def test_non_numeric(self):
        self.assertEqual(self.f("abc"), "abc")


class MetersFormatFilterTests(TestCase):
    """Tests for the meters_format template filter."""

    def setUp(self):
        from core.templatetags.core_tags import meters_format

        self.f = meters_format

    def test_format(self):
        self.assertEqual(self.f(3.456), "3.5 m")

    def test_non_numeric(self):
        self.assertEqual(self.f("abc"), "abc")


class DictGetFilterTests(TestCase):
    """Tests for the dict_get template filter."""

    def setUp(self):
        from core.templatetags.core_tags import dict_get

        self.f = dict_get

    def test_existing_key(self):
        self.assertEqual(self.f({"a": 1}, "a"), 1)

    def test_missing_key(self):
        self.assertEqual(self.f({"a": 1}, "b"), "")

    def test_non_dict(self):
        self.assertEqual(self.f("not a dict", "a"), "")

    def test_none_input(self):
        self.assertEqual(self.f(None, "key"), "")

    def test_integer_key(self):
        self.assertEqual(self.f({42: "value"}, 42), "value")


class StripPortFilterTests(TestCase):
    """Tests for the strip_port template filter."""

    def setUp(self):
        from core.templatetags.core_tags import strip_port

        self.f = strip_port

    def test_with_port(self):
        self.assertEqual(self.f("http://192.168.1.100:7125"), "http://192.168.1.100")

    def test_without_port(self):
        self.assertEqual(self.f("http://192.168.1.100"), "http://192.168.1.100")

    def test_empty(self):
        self.assertEqual(self.f(""), "")

    def test_none(self):
        self.assertEqual(self.f(None), "")


class WidgetClassFilterTests(TestCase):
    """Tests for the widget_class template filter."""

    def setUp(self):
        from core.templatetags.core_tags import widget_class

        self.f = widget_class

    def test_text_input(self):
        from django import forms

        form = forms.Form()
        field = forms.CharField()
        field.widget = forms.TextInput()
        # Simulate a BoundField
        form.fields["test"] = field
        bound = form["test"]
        self.assertEqual(self.f(bound), "TextInput")

    def test_non_field(self):
        self.assertEqual(self.f("not a field"), "")
