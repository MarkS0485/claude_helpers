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


NOW = 1_780_000_000.0  # arbitrary fixed epoch for history tests


def make_history(minutes_and_pcts, key="five_hour"):
    """[(minutes_ago, pct), ...] -> history sample list."""
    return [{"ts": NOW - m * 60, key: p} for m, p in minutes_and_pcts]


class UsedInWindowTests(unittest.TestCase):
    def test_simple_growth(self):
        hist = make_history([(20, 10.0), (10, 12.0), (0, 15.0)])
        self.assertAlmostEqual(cu.used_in_window(hist, "five_hour", 15, NOW), 5.0)

    def test_anchor_before_window(self):
        # sample at -20m anchors the 15m window: only growth after -15m counts
        hist = make_history([(20, 10.0), (5, 14.0), (0, 15.0)])
        self.assertAlmostEqual(cu.used_in_window(hist, "five_hour", 15, NOW), 5.0)

    def test_reset_mid_window_not_negative(self):
        # 90 -> 95 -> reset to 2 -> 4: consumed 5 + 2, not 95-90-91
        hist = make_history([(16, 90.0), (8, 95.0), (4, 2.0), (0, 4.0)])
        self.assertAlmostEqual(cu.used_in_window(hist, "five_hour", 15, NOW), 7.0)

    def test_insufficient_history(self):
        hist = make_history([(5, 10.0), (0, 12.0)])  # only 5 minutes deep
        self.assertIsNone(cu.used_in_window(hist, "five_hour", 60, NOW))

    def test_missing_bucket_key(self):
        hist = make_history([(20, 10.0), (0, 15.0)], key="seven_day")
        self.assertIsNone(cu.used_in_window(hist, "five_hour", 15, NOW))


class PacePerHourTests(unittest.TestCase):
    def test_uses_longest_window_available(self):
        # 70 minutes of history at a steady 6%/h
        hist = make_history([(m, 10.0 + (70 - m) * 0.1) for m in range(70, -1, -10)])
        self.assertAlmostEqual(cu.pace_per_hour(hist, "five_hour", NOW), 6.0)

    def test_falls_back_to_short_window(self):
        hist = make_history([(15, 10.0), (8, 11.0), (0, 12.0)])
        # 2% over 15m -> 8%/h
        self.assertAlmostEqual(cu.pace_per_hour(hist, "five_hour", NOW), 8.0)

    def test_no_history(self):
        self.assertIsNone(cu.pace_per_hour([], "five_hour", NOW))


class ForecastTests(unittest.TestCase):
    def test_gathering_data(self):
        out = plain(cu.forecast(15.0, None, NOW + 7200, NOW))
        self.assertIn("gathering", out)

    def test_idle(self):
        out = plain(cu.forecast(15.0, 0.0, NOW + 7200, NOW))
        self.assertIn("idle", out)

    def test_on_pace(self):
        # 10%/h with 2h left from 15% -> ~35% at reset
        out = plain(cu.forecast(15.0, 10.0, NOW + 7200, NOW))
        self.assertIn("on pace", out)
        self.assertIn("~35% at reset", out)

    def test_bust_recommends_slowdown(self):
        # 30%/h with 4h left from 40%: busts in 2h, sustainable is 15%/h (cut 50%)
        out = plain(cu.forecast(40.0, 30.0, NOW + 4 * 3600, NOW))
        self.assertIn("LIMIT BUST", out)
        self.assertIn("2h 00m before reset", out)
        self.assertIn("slow to <=15.0%/h", out)
        self.assertIn("cut 50%", out)


class RecordSampleTests(unittest.TestCase):
    def test_records_present_buckets_and_trims(self):
        old = {"ts": NOW - (cu.HISTORY_RETENTION_HOURS + 1) * 3600, "five_hour": 1.0}
        hist = cu.record_sample([old], SAMPLE, NOW)
        self.assertEqual(len(hist), 1)  # old sample trimmed
        self.assertEqual(hist[0]["five_hour"], 15.0)
        self.assertEqual(hist[0]["seven_day"], 54.0)
        self.assertNotIn("seven_day_opus", hist[0])  # null bucket not recorded


class RenderWithHistoryTests(unittest.TestCase):
    def test_render_includes_windows_and_forecast(self):
        hist = make_history([(20, 10.0), (10, 12.0), (0, 15.0)])
        out = plain(cu.render(SAMPLE, hist, NOW))
        self.assertIn("last 15m +5.0%", out)
        self.assertIn("60m -", out)  # not enough history for the 60m window

    def test_render_without_history_still_works(self):
        out = plain(cu.render(SAMPLE))
        self.assertIn("Session (5h)", out)
        self.assertNotIn("last 15m", out)


if __name__ == "__main__":
    unittest.main()
