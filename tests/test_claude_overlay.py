"""Unit tests for claude_overlay.py — pure helpers only, no GUI, no network."""

import unittest

import claude_overlay as co

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
}


class PickBucketsTests(unittest.TestCase):
    def test_skips_null_buckets_and_keeps_order(self):
        picked = co.pick_buckets(SAMPLE)
        self.assertEqual([k for k, _, _ in picked],
                         ["five_hour", "seven_day", "seven_day_sonnet"])

    def test_caps_at_three_segments(self):
        data = dict(SAMPLE, seven_day_opus={"utilization": 1.0,
                                            "resets_at": "2026-06-07T10:59:59+00:00"})
        picked = co.pick_buckets(data)
        self.assertEqual(len(picked), 3)
        self.assertEqual([k for k, _, _ in picked],
                         ["five_hour", "seven_day", "seven_day_opus"])

    def test_handles_no_data(self):
        self.assertEqual(co.pick_buckets(None), [])
        self.assertEqual(co.pick_buckets({}), [])

    def test_skips_bucket_with_null_utilization(self):
        data = {"five_hour": {"utilization": None, "resets_at": "x"}}
        self.assertEqual(co.pick_buckets(data), [])


class SegmentBoundsTests(unittest.TestCase):
    def test_three_equal_slices_cover_the_width(self):
        bounds = co.segment_bounds(3000, count=3, gap=2)
        self.assertEqual(bounds[0][0], 0)
        self.assertEqual(bounds[-1][1], 3000)
        # contiguous edges, separated only by the gap
        self.assertEqual(bounds[1][0] - bounds[0][1], 2)
        self.assertEqual(bounds[2][0] - bounds[1][1], 2)
        # equal widths bar the gap
        self.assertEqual(bounds[0][1] - bounds[0][0], 1000)

    def test_odd_width_still_monotonic(self):
        bounds = co.segment_bounds(1921, count=3, gap=2)
        for x0, x1 in bounds:
            self.assertLess(x0, x1)
        self.assertEqual(bounds[-1][1], 1921)


class FillWidthTests(unittest.TestCase):
    def test_proportional(self):
        self.assertEqual(co.fill_width(0, 600), 0)
        self.assertEqual(co.fill_width(50, 600), 300)
        self.assertEqual(co.fill_width(100, 600), 600)

    def test_clamps_out_of_range(self):
        self.assertEqual(co.fill_width(250, 600), 600)
        self.assertEqual(co.fill_width(-5, 600), 0)


class ParseEdgesTests(unittest.TestCase):
    def test_numeric_modes(self):
        self.assertEqual(co.parse_edges("0"), ("bottom",))
        self.assertEqual(co.parse_edges("1"), ("top",))
        self.assertEqual(co.parse_edges("2"), ("left",))
        self.assertEqual(co.parse_edges("3"), ("right",))
        self.assertEqual(co.parse_edges("4"),
                         ("bottom", "top", "left", "right"))
        self.assertEqual(co.parse_edges("5"), ("left", "right"))
        self.assertEqual(co.parse_edges("6"), ("top", "bottom"))

    def test_names_still_accepted(self):
        self.assertEqual(co.parse_edges("bottom"), ("bottom",))
        self.assertEqual(co.parse_edges("RIGHT"), ("right",))

    def test_unknown_raises(self):
        for bad in ("7", "-1", "diagonal", ""):
            with self.assertRaises(ValueError):
                co.parse_edges(bad)


class InsetAreaTests(unittest.TestCase):
    AREA = (0, 0, 2560, 1380)

    def test_sides_give_way_to_top_and_bottom(self):
        all_edges = ("bottom", "top", "left", "right")
        self.assertEqual(co.inset_area(self.AREA, all_edges, "left", 4),
                         (0, 4, 2560, 1376))
        self.assertEqual(co.inset_area(self.AREA, all_edges, "right", 4),
                         (0, 4, 2560, 1376))

    def test_horizontal_strips_never_inset(self):
        all_edges = ("bottom", "top", "left", "right")
        self.assertEqual(co.inset_area(self.AREA, all_edges, "bottom", 4),
                         self.AREA)
        self.assertEqual(co.inset_area(self.AREA, all_edges, "top", 4),
                         self.AREA)

    def test_lone_side_keeps_full_height(self):
        self.assertEqual(co.inset_area(self.AREA, ("left",), "left", 4),
                         self.AREA)

    def test_side_with_only_bottom(self):
        self.assertEqual(
            co.inset_area(self.AREA, ("bottom", "right"), "right", 4),
            (0, 0, 2560, 1376))


class GeometryForTests(unittest.TestCase):
    AREA = (0, 0, 2560, 1380)  # 2560x1440 screen, 60px taskbar at the bottom

    def test_bottom_sits_on_the_taskbar_edge(self):
        self.assertEqual(co.geometry_for("bottom", self.AREA, 4),
                         (2560, 4, 0, 1376))

    def test_top(self):
        self.assertEqual(co.geometry_for("top", self.AREA, 4),
                         (2560, 4, 0, 0))

    def test_left_and_right_run_vertically(self):
        self.assertEqual(co.geometry_for("left", self.AREA, 4),
                         (4, 1380, 0, 0))
        self.assertEqual(co.geometry_for("right", self.AREA, 4),
                         (4, 1380, 2556, 0))

    def test_offset_work_area(self):
        # taskbar on the left: work area starts at x=60
        area = (60, 0, 2560, 1440)
        self.assertEqual(co.geometry_for("left", area, 2), (2, 1440, 60, 0))
        self.assertEqual(co.geometry_for("bottom", area, 2),
                         (2500, 2, 60, 1438))

    def test_unknown_edge_raises(self):
        with self.assertRaises(ValueError):
            co.geometry_for("diagonal", self.AREA, 4)


class AxisToCanvasTests(unittest.TestCase):
    def test_bottom_is_identity(self):
        self.assertEqual(co.axis_to_canvas("bottom", 900, 0, 100), (0, 100))

    def test_top_is_the_bottom_mirrored(self):
        # the first segment (axis 0..100) lands at the far canvas end:
        # bottom-left becomes top-right, the middle stays the middle
        self.assertEqual(co.axis_to_canvas("top", 900, 0, 100), (800, 900))
        self.assertEqual(co.axis_to_canvas("top", 900, 400, 500), (400, 500))

    def test_sides_fill_upwards(self):
        # axis 0 is the bottom of the screen; canvas y grows downward
        self.assertEqual(co.axis_to_canvas("left", 1380, 0, 100),
                         (1280, 1380))
        self.assertEqual(co.axis_to_canvas("right", 1380, 1280, 1380),
                         (0, 100))


class GradientColourTests(unittest.TestCase):
    def test_endpoints_and_midpoint(self):
        self.assertEqual(co.gradient_colour(0.0), "#00ff00")   # neon green
        self.assertEqual(co.gradient_colour(0.5), "#ffff00")   # yellow
        self.assertEqual(co.gradient_colour(1.0), "#ff0000")   # pure red

    def test_clamps_fraction(self):
        self.assertEqual(co.gradient_colour(-0.5), "#00ff00")
        self.assertEqual(co.gradient_colour(2.0), "#ff0000")

    def test_brightness_scales_channels(self):
        self.assertEqual(co.gradient_colour(0.0, brightness=0.2), "#003300")
        self.assertEqual(co.gradient_colour(1.0, brightness=0.0), "#000000")
        self.assertEqual(co.gradient_colour(1.0, brightness=5.0), "#ff0000")


class GradientRunsTests(unittest.TestCase):
    def test_runs_cover_exactly_the_fill(self):
        runs = co.gradient_runs(600, 300)
        self.assertEqual(runs[0][0], 0)
        self.assertEqual(runs[-1][1], 300)
        for (a, b) in zip(runs, runs[1:]):  # contiguous, no gaps or overlap
            self.assertEqual(a[1], b[0])

    def test_empty_when_nothing_to_fill(self):
        self.assertEqual(co.gradient_runs(600, 0), [])
        self.assertEqual(co.gradient_runs(0, 100), [])

    def test_gradient_spans_segment_not_fill(self):
        # a half-full bar ends at yellow; only a full bar reaches red
        half = co.gradient_runs(600, 300)
        full = co.gradient_runs(600, 600)
        r, g, _ = (int(half[-1][2][i:i + 2], 16) for i in (1, 3, 5))
        self.assertGreaterEqual(r, 250)  # ~yellow tip at 50% (band colour is
        self.assertEqual(g, 255)         # sampled at the band's midpoint)
        r, g, _ = (int(full[-1][2][i:i + 2], 16) for i in (1, 3, 5))
        self.assertEqual(r, 255)
        self.assertLess(g, 8)  # effectively pure red at 100%
        self.assertEqual(half[0][2], full[0][2])  # both start neon green

    def test_run_count_capped_by_steps_and_width(self):
        self.assertLessEqual(len(co.gradient_runs(2000, 2000)), co.GRADIENT_STEPS)
        runs = co.gradient_runs(10, 10)  # narrower than the step count
        self.assertLessEqual(len(runs), 10)
        self.assertEqual(runs[-1][1], 10)

    def test_brightness_passed_through(self):
        dimmed = co.gradient_runs(600, 600, brightness=0.2)
        self.assertEqual(dimmed[0][2], "#003300")


if __name__ == "__main__":
    unittest.main()
