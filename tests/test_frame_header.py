import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEV_SRC = REPO_ROOT / "dev" / "src"
os.environ.setdefault("WORK_DIR", tempfile.mkdtemp(prefix="frame_header_import_"))
sys.path.insert(0, str(DEV_SRC / "common"))
sys.path.insert(0, str(DEV_SRC / "youtube"))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dev_script = load_module("frame_header_dev_script", DEV_SRC / "common" / "0_script.py")
dev_frame_style = load_module("frame_header_dev_style", DEV_SRC / "youtube" / "frame_style.py")


TOP_STYLE = {
    "height_pct": "13.5",
    "title": "기억경고",
    "font": "Noto Sans CJK KR",
    "font_style": "Bold",
    "title_color": "white",
    "title_size": "84",
    "subtitle_color": "white",
    "subtitle_size": "48",
    "top_margin_pct": "23",
    "bottom_margin_pct": "23",
    "side_margin_pct": "6",
}


class FrameHeaderNormalizationTests(unittest.TestCase):
    def test_dev_preserves_complete_subtitle_beyond_old_limit(self):
        subtitle = "잊힌 게 아니라 뇌가 한창 자라던 시기였기 때문입니다"
        result = {"frame_header": {"title": "성장의비밀", "subtitle": subtitle}}
        normalized = dev_script.normalize_frame_header(result, {}, [])
        self.assertEqual(normalized["subtitle"], subtitle)

    def test_abnormal_output_uses_only_generous_safety_caps(self):
        result = {"frame_header": {"title": "가" * 25, "subtitle": "나" * 50}}
        normalized = dev_script.normalize_frame_header(result, {}, [])
        self.assertEqual(len(normalized["title"]), 20)
        self.assertEqual(len(normalized["subtitle"]), 40)


class FrameHeaderWrappingTests(unittest.TestCase):
    def test_top_margin_moves_header_text_down_without_changing_header_height(self):
        data = {
            **TOP_STYLE,
            "subtitle": "오늘의 뇌건강",
            "title_size": "60",
            "subtitle_size": "40",
            "top_margin_pct": "10",
            "bottom_margin_pct": "5",
        }
        base = dev_frame_style.resolve_top(data)
        moved_down = dev_frame_style.resolve_top({**data, "top_margin_pct": "20"})
        self.assertEqual(base["height_px"], moved_down["height_px"])
        self.assertEqual(base["title_size"], moved_down["title_size"])
        self.assertEqual(base["subtitle_size"], moved_down["subtitle_size"])
        self.assertEqual(moved_down["top_margin_px"], 52)
        self.assertEqual(moved_down["title_y"], base["title_y"] + 26)
        self.assertEqual(moved_down["subtitle_y"], base["subtitle_y"] + 26)

    def test_title_and_subtitle_colors_can_be_configured_independently(self):
        resolved = dev_frame_style.resolve_top(
            {
                **TOP_STYLE,
                "title_color": "yellow",
                "subtitle_color": "white",
            }
        )
        self.assertEqual(resolved["title_color"], "yellow")
        self.assertEqual(resolved["subtitle_color"], "white")

    def test_legacy_font_color_remains_the_fallback_for_both_lines(self):
        data = {
            key: value
            for key, value in TOP_STYLE.items()
            if key not in ("title_color", "subtitle_color")
        }
        resolved = dev_frame_style.resolve_top({**data, "font_color": "yellow"})
        self.assertEqual(resolved["title_color"], "yellow")
        self.assertEqual(resolved["subtitle_color"], "yellow")

    def test_normal_subtitle_remains_a_single_unchanged_line(self):
        data = {**TOP_STYLE, "subtitle": "오늘의 뇌건강"}
        resolved = dev_frame_style.resolve_top(data)
        self.assertEqual(resolved["subtitle"], "오늘의 뇌건강")
        self.assertEqual(resolved["subtitle_line_count"], 1)
        self.assertEqual((resolved["title_size"], resolved["subtitle_size"]), (72, 41))

    def test_long_subtitle_wraps_once_at_a_word_boundary(self):
        subtitle = "잊힌 게 아니라 뇌가 한창 자라던 시기였기 때문입니다"
        wrapped, line_count = dev_frame_style.wrap_subtitle_if_needed(subtitle, 48, 950)
        self.assertEqual(line_count, 2)
        self.assertEqual(wrapped.replace("\n", " "), subtitle)
        self.assertEqual(wrapped.count("\n"), 1)

    def test_wrapped_subtitle_stays_inside_the_top_frame(self):
        subtitle = "잊힌 게 아니라 뇌가 한창 자라던 시기였기 때문입니다"
        data = {**TOP_STYLE, "subtitle": subtitle}
        resolved = dev_frame_style.resolve_top(data)
        subtitle_bottom = resolved["subtitle_y"] + dev_frame_style.subtitle_block_height(
            resolved["subtitle_size"], resolved["subtitle_line_count"]
        )
        self.assertEqual(resolved["subtitle_line_count"], 2)
        self.assertLessEqual(subtitle_bottom, resolved["bottom_anchor_y"])
        self.assertLessEqual(subtitle_bottom, resolved["height_px"])

    def test_long_unbroken_word_is_preserved_and_scaled(self):
        subtitle = "가" * 40
        data = {**TOP_STYLE, "subtitle": subtitle}
        resolved = dev_frame_style.resolve_top(data)
        self.assertEqual(resolved["subtitle"], subtitle)
        self.assertEqual(resolved["subtitle_line_count"], 1)
        self.assertLessEqual(
            dev_frame_style.max_line_width_units(resolved["subtitle"]) * resolved["subtitle_size"],
            dev_frame_style.CANVAS_W - resolved["side_margin_px"] * 2,
        )


class FrameBottomStyleTests(unittest.TestCase):
    def test_dev_bottom_uses_simple_keys_and_bold_style(self):
        presets = dev_frame_style.load_presets(REPO_ROOT / "dev" / "frame_bottom_styles.yaml")
        resolved = dev_frame_style.resolve_bottom(dev_frame_style.resolve(presets, "default"))
        self.assertEqual(resolved["height_px"], 518)
        self.assertEqual(resolved["background"], "black")
        self.assertEqual(resolved["channel"], "브레인피프티")
        self.assertEqual(resolved["font"], "Noto Sans CJK KR")
        self.assertEqual(resolved["font_style"], "Bold")
        self.assertEqual(resolved["color"], "white")
        self.assertEqual(resolved["size"], 60)
        self.assertEqual(resolved["top_margin_px"], 20)
        self.assertEqual(resolved["channel_y"], 1422)

    def test_minimal_bottom_hides_the_channel(self):
        presets = dev_frame_style.load_presets(REPO_ROOT / "dev" / "frame_bottom_styles.yaml")
        resolved = dev_frame_style.resolve_bottom(dev_frame_style.resolve(presets, "minimal"))
        self.assertEqual(resolved["channel"], "")
        self.assertEqual(resolved["font_style"], "Bold")


if __name__ == "__main__":
    unittest.main()
