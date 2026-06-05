"""Unit tests for claude_usage.py — pure functions only, no network."""

import re
import unittest
from datetime import datetime, timedelta, timezone

import claude_usage as cu

ANSI = re.compile(r"\x1b\[[0-9;]*m")

SAMPLE = {
    "five_hour": {
        "utilization": 15.0,
        "resets_at": "2026-06-05T15:10:00.936507+00:00",
    },
    "seven_day": {
        "utilization": 54.0,
        "resets_at": "2026-06-07T10:59:59.936529+00:00",
    },
    "seven_day_opus": None,
    "seven_day_sonnet": {
        "utilization": 12.0,
        "resets_at": "2026-06-07T10:59:59.936536+00:00",
    },
    "extra_usage": {
        "is_enabled": True,
        "monthly_limit": 2000,
        "used_credits": 0.0,
        "currency": "GBP",
    },
}


def plain(text):
    """Strip ANSI escapes so assertions see what the user sees."""
    return ANSI.sub("", text)


class ColourForTests(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(cu.colour_for(0), cu.GREEN)
        self.assertEqual(cu.colour_for(49.9), cu.GREEN)
        self.assertEqual(cu.colour_for(50), cu.YELLOW)
        self.assertEqual(cu.colour_for(79.9), cu.YELLOW)
        self.assertEqual(cu.colour_for(80), cu.RED)
        self.assertEqual(cu.colour_for(100), cu.RED)


class BarTests(unittest.TestCase):
    def test_fill_proportions(self):
        self.assertEqual(plain(cu.bar(0, width=10)), "░" * 10)
        self.assertEqual(plain(cu.bar(100, width=10)), "█" * 10)
        self.assertEqual(plain(cu.bar(50, width=10)), "█" * 5 + "░" * 5)

    def test_clamps_over_100(self):
        self.assertEqual(plain(cu.bar(250, width=10)), "█" * 10)


class FmtResetTests(unittest.TestCase):
    def test_future_reset_has_countdown(self):
        when = datetime.now(timezone.utc) + timedelta(hours=2, minutes=30)
        out = plain(cu.fmt_reset(when.isoformat()))
        self.assertIn("resets in 2h", out)

    def test_days_granularity(self):
        when = datetime.now(timezone.utc) + timedelta(days=1, hours=22, minutes=5)
        out = plain(cu.fmt_reset(when.isoformat()))
        self.assertIn("resets in 1d 22h", out)

    def test_past_reset_clamps_to_zero(self):
        when = datetime.now(timezone.utc) - timedelta(hours=1)
        out = plain(cu.fmt_reset(when.isoformat()))
        self.assertIn("resets in 0m", out)


class RenderTests(unittest.TestCase):
    def test_renders_present_buckets(self):
        out = plain(cu.render(SAMPLE))
        self.assertIn("Session (5h)", out)
        self.assertIn("15.0%", out)
        self.assertIn("Weekly (all)", out)
        self.assertIn("54.0%", out)
        self.assertIn("Weekly Sonnet", out)

    def test_omits_null_buckets(self):
        out = plain(cu.render(SAMPLE))
        self.assertNotIn("Weekly Opus", out)

    def test_extra_usage_line(self):
        out = plain(cu.render(SAMPLE))
        self.assertIn("0.00 / 2000 GBP", out)

    def test_extra_usage_hidden_when_disabled(self):
        data = dict(SAMPLE, extra_usage={"is_enabled": False})
        self.assertNotIn("Extra usage", plain(cu.render(data)))

    def test_null_utilization_treated_as_zero(self):
        data = dict(SAMPLE, five_hour={"utilization": None,
                                       "resets_at": SAMPLE["five_hour"]["resets_at"]})
        out = plain(cu.render(data))
        self.assertIn("0.0%", out)


if __name__ == "__main__":
    unittest.main()
