import importlib.util
import json
import random
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("broll_policy", ROOT / "dev" / "src" / "youtube" / "broll_policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


def video(video_id, width, height, duration=10, fps=30):
    return {
        "id": video_id,
        "duration": duration,
        "video_files": [{
            "width": width, "height": height, "fps": fps,
            "link": f"https://example/{video_id}.mp4",
        }],
    }


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

    def test_normalization_filter_speeds_up_playback_before_scaling(self):
        value = policy.normalization_filter("cover", 5, speed=1.2)
        self.assertTrue(value.startswith("setpts=PTS/1.2"))
        # A speed of exactly 1.0 must not emit a no-op filter.
        self.assertNotIn("setpts", policy.normalization_filter("cover", 5, speed=1.0))
        self.assertTrue(policy.normalization_filter("blur-contain", 5, speed=1.2).startswith("setpts="))

    def test_clip_used_by_a_previous_job_loses_to_a_fresh_one(self):
        videos = [video(1, 1080, 1920), video(2, 1080, 1920)]
        selected = policy.select_video(
            videos, 5, set(), [], random.Random(1), recent_video_ids=[1],
        )
        self.assertEqual(selected["video"]["id"], 2)

    def test_long_ambient_clip_loses_to_a_shorter_dynamic_one(self):
        videos = [video(1, 1080, 1920, duration=60), video(2, 1080, 1920, duration=10)]
        selected = policy.select_video(videos, 5, set(), [], random.Random(1))
        self.assertEqual(selected["video"]["id"], 2)

    def test_higher_fps_rendition_is_preferred_for_smooth_speedup(self):
        candidate = {
            "id": 1, "duration": 10,
            "video_files": [
                {"width": 1080, "height": 1920, "fps": 24, "link": "https://example/24.mp4"},
                {"width": 1080, "height": 1920, "fps": 60, "link": "https://example/60.mp4"},
            ],
        }
        self.assertEqual(policy.choose_video_file(candidate)["fps"], 60)

    def test_history_round_trips_and_keeps_newest_first_within_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broll_usage.json"
            self.assertEqual(policy.load_recent_video_ids(path), [])

            policy.record_used_video_ids([1, 2], path=path, limit=3)
            policy.record_used_video_ids([3, 2], path=path, limit=3)

            self.assertEqual(policy.load_recent_video_ids(path), [3, 2, 1])
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["recent"], [3, 2, 1])

    def test_unreadable_history_is_ignored_instead_of_failing_the_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broll_usage.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(policy.load_recent_video_ids(path), [])


if __name__ == "__main__":
    unittest.main()