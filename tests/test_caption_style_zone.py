import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEV_SRC = REPO_ROOT / "dev" / "src"
sys.path.insert(0, str(DEV_SRC / "youtube"))

import caption_style as cs  # noqa: E402


class ZonePositionTests(unittest.TestCase):
    def test_frame_bottom_bottom_align(self):
        style = {
            "PositionMode": "zone", "Zone": "frame_bottom",
            "XPct": "50", "VAlign": "bottom", "DistancePct": "15",
            "ZoneTopH": "240", "ZoneBottomH": "360",
        }
        self.assertEqual(cs.position_override(style), r"{\an2\pos(540,1866)}")

    def test_frame_top_top_align(self):
        style = {
            "PositionMode": "zone", "Zone": "frame_top",
            "XPct": "50", "VAlign": "top", "DistancePct": "10",
            "ZoneTopH": "240", "ZoneBottomH": "360",
        }
        self.assertEqual(cs.position_override(style), r"{\an8\pos(540,24)}")

    def test_broll_zone_spans_full_canvas_in_full_mode(self):
        style = {
            "PositionMode": "zone", "Zone": "broll",
            "XPct": "50", "VAlign": "bottom", "DistancePct": "5",
            "ZoneTopH": "0", "ZoneBottomH": "0",
        }
        self.assertEqual(cs.position_override(style), r"{\an2\pos(540,1824)}")

    def test_zero_height_zone_raises(self):
        style = {
            "PositionMode": "zone", "Zone": "frame_top",
            "ZoneTopH": "0", "ZoneBottomH": "0",
        }
        with self.assertRaises(SystemExit):
            cs.position_override(style)


if __name__ == "__main__":
    unittest.main()
