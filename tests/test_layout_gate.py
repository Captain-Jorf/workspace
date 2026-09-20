"""Deterministic layout collision + density gate (issue #32 §5/§6/§11).

The gate is a pure function of declared items (bbox + z-layer + kind):
intersecting text boxes, hidden text, labels behind foreground, lines through
text, microtext, excessive density and safe-zone intrusion must all be
blocked BEFORE a frame renders. Decorative elements may touch text only below
the documented opacity. Nested containment (label → chip → card) is legal.
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "build"))

import layout_gate as lg  # noqa: E402


def item(iid, kind, bbox, z=lg.LAYER_DIAGRAM, **kw):
    it = {"id": iid, "kind": kind, "bbox": list(bbox), "z": z}
    it.update(kw)
    return it


class TextCollisions(unittest.TestCase):
    def test_intersecting_text_boxes_are_blocked(self):
        issues = lg.check_items([
            item("a", "text", (100, 100, 400, 160), lg.LAYER_LABEL,
                 text="DRAFT", font_px=30, color=(255, 228, 158), bg=(16, 13, 10)),
            item("b", "text", (300, 120, 600, 180), lg.LAYER_LABEL,
                 text="NOD", font_px=30, color=(255, 228, 158), bg=(16, 13, 10)),
        ])
        self.assertTrue(any("text overlap" in i for i in issues), issues)

    def test_clear_text_passes(self):
        issues = lg.check_items([
            item("a", "text", (100, 100, 400, 160), lg.LAYER_LABEL,
                 text="LISTEN", font_px=30, color=(255, 228, 158), bg=(16, 13, 10)),
            item("b", "text", (600, 100, 900, 160), lg.LAYER_LABEL,
                 text="DRAFT", font_px=30, color=(255, 228, 158), bg=(16, 13, 10)),
        ])
        self.assertEqual(issues, [])

    def test_hidden_text_behind_unrelated_foreground_is_blocked(self):
        issues = lg.check_items([
            item("bar", "bar", (100, 100, 700, 200), group="g"),
            item("lab", "text", (120, 120, 400, 180), lg.LAYER_LABEL,
                 text="RESIDUE", font_px=26, color=(255, 228, 158), bg=(150, 106, 44)),
        ])
        self.assertTrue(any("hidden behind" in i or "crossing another" in i
                            for i in issues), issues)

    def test_label_inside_its_own_parent_is_legal(self):
        issues = lg.check_items([
            item("bar", "bar", (100, 100, 700, 200), group="g"),
            item("lab", "text", (120, 120, 400, 180), lg.LAYER_LABEL, parent="bar",
                 text="TASK A", font_px=26, color=(20, 16, 12), bg=(233, 180, 74)),
        ])
        self.assertEqual(issues, [])

    def test_nested_containment_label_chip_card_is_legal(self):
        # issue #32 §5 regression: text inside a chip inside a phone/card must
        # NOT be flagged as hidden behind the outer element (parent-chain walk)
        issues = lg.check_items([
            item("phone", "card", (110, 100, 440, 740), group="notif"),
            item("ping", "chip", (140, 210, 410, 310), parent="phone", group="notif"),
            item("ping_text", "text", (170, 240, 380, 280), lg.LAYER_LABEL,
                 parent="ping", group="notif", text="NEW MESSAGE", font_px=26,
                 color=(255, 228, 158), bg=(40, 32, 22)),
        ])
        self.assertEqual(issues, [])

    def test_label_not_fully_inside_parent_is_blocked(self):
        issues = lg.check_items([
            item("chip", "chip", (100, 100, 300, 160)),
            item("lab", "text", (120, 120, 420, 150), lg.LAYER_LABEL, parent="chip",
                 text="TOO LONG FOR THE BOX", font_px=26,
                 color=(255, 228, 158), bg=(26, 20, 13)),
        ])
        self.assertTrue(any("not fully inside its parent" in i for i in issues), issues)


class LinesThroughText(unittest.TestCase):
    def test_line_crossing_semantic_text_is_blocked(self):
        issues = lg.check_items([
            item("ln", "line", (100, 96, 900, 504), endpoints=[(100, 100), (900, 500)]),
            item("t", "text", (400, 250, 600, 320), lg.LAYER_LABEL,
                 text="THE GAP", font_px=30, color=(255, 228, 158), bg=(16, 13, 10)),
        ])
        self.assertTrue(any("crosses text" in i for i in issues), issues)

    def test_arrow_toward_its_own_endpoint_label_is_legal(self):
        # the segment DOES cross this text's bbox — it must be exempt because
        # the text is the line's own endpoint label (same group, near the end)
        issues = lg.check_items([
            item("ln", "line", (100, 416, 400, 424), group="chain",
                 endpoints=[(100, 420), (400, 420)]),
            item("t", "text", (330, 405, 420, 445), lg.LAYER_LABEL, group="chain",
                 text="NEXT", font_px=26, color=(255, 228, 158), bg=(16, 13, 10)),
        ])
        self.assertEqual(issues, [])

    def test_line_crossing_unrelated_card_is_blocked(self):
        issues = lg.check_items([
            item("card", "card", (200, 200, 800, 600), group="a"),
            item("ln", "line", (100, 396, 900, 404), group="b",
                 endpoints=[(100, 400), (900, 400)]),
        ])
        self.assertTrue(any("crosses card" in i for i in issues), issues)


class DecorationOpacity(unittest.TestCase):
    def test_decoration_above_opacity_limit_may_not_touch_text(self):
        issues = lg.check_items([
            item("dec", "decoration", (300, 300, 700, 400),
                 lg.LAYER_DECORATION, opacity=0.8),
            item("t", "text", (400, 320, 600, 380), lg.LAYER_LABEL,
                 text="NOTE", font_px=26, color=(255, 228, 158), bg=(16, 13, 10)),
        ])
        self.assertTrue(any("documented opacity" in i for i in issues), issues)

    def test_subtle_decoration_behind_text_is_legal(self):
        issues = lg.check_items([
            item("ring", "decoration", (190, 145, 890, 715),
                 lg.LAYER_DECORATION, opacity=0.18),
            item("chip", "chip", (410, 111, 670, 179), group="loop"),
            item("t", "text", (450, 130, 630, 160), lg.LAYER_LABEL, parent="chip",
                 group="loop", text="PAUSE", font_px=28,
                 color=(255, 228, 158), bg=(26, 20, 13)),
        ])
        self.assertEqual(issues, [])


class ReadabilityAndDensity(unittest.TestCase):
    def test_microtext_is_blocked(self):
        issues = lg.check_items([
            item("t", "text", (100, 100, 300, 120), lg.LAYER_LABEL,
                 text="TINY", font_px=14, color=(255, 228, 158), bg=(16, 13, 10)),
        ])
        self.assertTrue(any("microtext" in i for i in issues), issues)

    def test_label_below_readable_minimum_is_blocked(self):
        issues = lg.check_items([
            item("t", "text", (100, 100, 300, 130), lg.LAYER_LABEL,
                 text="SMALL", font_px=20, color=(255, 228, 158), bg=(16, 13, 10)),
        ])
        self.assertTrue(any("below the readable minimum" in i for i in issues), issues)

    def test_low_contrast_is_blocked(self):
        issues = lg.check_items([
            item("t", "text", (100, 100, 300, 140), lg.LAYER_LABEL,
                 text="GREY ON GREY", font_px=30, color=(120, 110, 100),
                 bg=(130, 120, 110)),
        ])
        self.assertTrue(any("contrast" in i for i in issues), issues)

    def test_too_many_foreground_components_is_blocked(self):
        items = [item(f"c{i}", "bar", (i * 10, 0, i * 10 + 8, 40), group="x")
                 for i in range(lg.MAX_FOREGROUND_COMPONENTS + 1)]
        issues = lg.check_items(items)
        self.assertTrue(any("foreground components" in i for i in issues), issues)

    def test_too_many_labels_is_blocked(self):
        items = [item(f"t{i}", "text", (i * 200, 0, i * 200 + 120, 40), lg.LAYER_LABEL,
                      text=f"L{i}", font_px=26, color=(255, 228, 158), bg=(16, 13, 10))
                 for i in range(lg.MAX_TEXT_LABELS + 1)]
        issues = lg.check_items(items)
        self.assertTrue(any("text labels" in i for i in issues), issues)

    def test_foreground_area_cap_is_enforced(self):
        issues = lg.check_items([
            item("huge", "card", (20, 20, 1060, 820)),
        ])
        self.assertTrue(any("no whitespace left" in i for i in issues), issues)

    def test_density_report_is_measurable(self):
        rep = lg.scene_density_report([
            item("c", "card", (100, 100, 500, 400), group="g"),
            item("t", "text", (150, 150, 400, 200), lg.LAYER_LABEL, parent="c",
                 text="X", font_px=30, color=(255, 228, 158), bg=(23, 18, 12)),
        ])
        self.assertEqual(rep["foreground_components"], 1)
        self.assertEqual(rep["text_labels"], 1)
        self.assertEqual(rep["min_label_px"], 30)
        self.assertGreater(rep["foreground_area_fraction"], 0.0)
        self.assertLess(rep["foreground_area_fraction"], 1.0)


class SafeZones(unittest.TestCase):
    def test_item_entering_subtitle_band_is_blocked(self):
        safe = [("subtitle", (0, 0, 1080, 444)), ("bottom-reserved", (0, 1450, 1080, 1920))]
        issues = lg.check_items([
            item("bar", "bar", (100, 400, 900, 600), group="g"),
        ], safe_boxes=safe)
        self.assertTrue(any("subtitle" in i for i in issues), issues)

    def test_chrome_items_may_live_in_safe_zones(self):
        safe = [("subtitle", (0, 0, 1080, 444)), ("bottom-reserved", (0, 1450, 1080, 1920))]
        issues = lg.check_items([
            item("sub", "subtitle", (90, 200, 990, 430), lg.LAYER_SUBTITLE),
            item("hdl", "handle", (300, 1800, 780, 1850), lg.LAYER_CHROME),
        ], safe_boxes=safe)
        self.assertEqual(issues, [])

    def test_scene_zone_item_clear_of_both_bands_passes(self):
        safe = [("subtitle", (0, 0, 1080, 444)), ("bottom-reserved", (0, 1450, 1080, 1920))]
        issues = lg.check_items([
            item("bar", "bar", (100, 600, 900, 800), group="g"),
        ], safe_boxes=safe)
        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
