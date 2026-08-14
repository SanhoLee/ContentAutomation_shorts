import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEV_SRC = REPO_ROOT / "dev" / "src"
sys.path.insert(0, str(DEV_SRC / "youtube"))

import channel_style as chs  # noqa: E402


class ChannelZonePositionTests(unittest.TestCase):
    def test_frame_bottom_top_align(self):
        style = {
            "Zone": "frame_bottom", "XPct": "50", "VAlign": "top", "DistancePct": "4",
            "ZoneTopH": "0", "ZoneBottomH": "518",
        }
        x_expr, y_expr = chs.position_exprs(style)
        self.assertEqual(x_expr, "(1080*50/100)-text_w/2")
        self.assertEqual(y_expr, "(1402+(518)*4/100)")

    def test_broll_zone_spans_full_canvas_in_full_mode(self):
        style = {
            "Zone": "broll", "XPct": "92", "VAlign": "bottom", "DistancePct": "4",
            "ZoneTopH": "0", "ZoneBottomH": "0",
        }
        x_expr, y_expr = chs.position_exprs(style)
        self.assertEqual(x_expr, "(1080*92/100)-text_w/2")
        self.assertEqual(y_expr, "(1920-(1920)*4/100-text_h)")

    def test_zero_height_zone_raises(self):
        style = {"Zone": "frame_top", "ZoneTopH": "0", "ZoneBottomH": "0"}
        with self.assertRaises(SystemExit):
            chs.position_exprs(style)


if __name__ == "__main__":
    unittest.main()
