import importlib.util
import random
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("broll_policy", ROOT / "dev" / "src" / "youtube" / "broll_policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def video(video_id, width, height, duration=10):
    return {"id": video_id, "duration": duration, "video_files": [{"width": width, "height": height, "link": f"https://example/{video_id}.mp4"}]}


class BrollPolicyTests(unittest.TestCase):
    def test_classifies_orientation(self):
        self.assertEqual(policy.orientation(1080, 1920), "portrait")
        self.assertEqual(policy.orientation(1920, 1080), "landscape")
        self.assertEqual(policy.orientation(1000, 1000), "square")

    def test_landscape_is_available_and_uses_blur_contain(self):
        selected = policy.select_video([video(1, 1920, 1080)], 5, set(), [])
        self.assertEqual(selected["orientation"], "landscape")
        self.assertEqual(selected["fit_mode"], "blur-contain")

    def test_portrait_remains_preferred_when_mix_is_below_target(self):
        videos = [video(1, 1080, 1920), video(2, 1920, 1080)]
        selected = policy.select_video(videos, 5, set(), [], random.Random(1))
        self.assertEqual(selected["orientation"], "portrait")

    def test_landscape_gets_balance_bonus_after_portrait_run(self):
        videos = [video(1, 1080, 1920), video(2, 1200, 900)]
        selected = policy.select_video(videos, 5, set(), ["portrait", "portrait"], random.Random(1))
        self.assertEqual(selected["orientation"], "landscape")

    def test_duration_and_duplicate_guards_outrank_orientation(self):
        videos = [video(1, 1080, 1920, duration=2), video(2, 1920, 1080, duration=12)]
        selected = policy.select_video(videos, 8, {1}, [], random.Random(1))
        self.assertEqual(selected["video"]["id"], 2)
        self.assertFalse(selected["short_allowed"])

    def test_normalization_filter_preserves_landscape_foreground(self):
        value = policy.normalization_filter("blur-contain", 5)
        self.assertIn("force_original_aspect_ratio=decrease", value)
        self.assertIn("boxblur", value)


if __name__ == "__main__":
    unittest.main()