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

    def test_normalization_never_retimes_the_clip(self):
        # Liveliness must come from the source footage, not from re-timing it.
        for mode in ("cover", "blur-contain"):
            self.assertNotIn("setpts", policy.normalization_filter(mode, 5))

    def test_slow_motion_clip_loses_to_a_normal_speed_one(self):
        normal = video(1, 1080, 1920)
        slowmo = video(2, 1080, 1920)
        slowmo["url"] = "https://www.pexels.com/video/slow-motion-of-a-woman-walking-12345/"
        normal["url"] = "https://www.pexels.com/video/a-woman-walking-on-the-street-67890/"
        selected = policy.select_video([slowmo, normal], 5, set(), [], random.Random(1))
        self.assertEqual(selected["video"]["id"], 1)

    def test_slow_motion_detection_reads_the_pexels_slug(self):
        self.assertTrue(policy.is_slow_motion({"url": "https://www.pexels.com/video/slow-motion-run-1/"}))
        self.assertTrue(policy.is_slow_motion({"url": "https://www.pexels.com/video/time-lapse-city-2/"}))
        self.assertFalse(policy.is_slow_motion({"url": "https://www.pexels.com/video/woman-cooking-3/"}))
        self.assertFalse(policy.is_slow_motion({}))

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

    def test_rendition_choice_ignores_fps_and_tracks_resolution(self):
        # Pexels serves every rendition of a video at the same fps, so fps must
        # not sway this pick; a 60fps low-res file is still the wrong file.
        candidate = {
            "id": 1, "duration": 10,
            "video_files": [
                {"width": 1280, "height": 720, "fps": 24, "link": "https://example/hd.mp4"},
                {"width": 426, "height": 240, "fps": 60, "link": "https://example/tiny.mp4"},
            ],
        }
        self.assertEqual(policy.choose_video_file(candidate)["link"], "https://example/hd.mp4")

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

    def test_recording_video_ids_preserves_other_history_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broll_usage.json"
            policy.record_query_page("woman cooking", 2, path=path)
            policy.record_used_video_ids([1, 2], path=path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(raw["recent"], [1, 2])
            self.assertEqual(raw["query_pages"]["woman cooking"], 2)

    def test_short_scene_stays_a_single_shot(self):
        self.assertEqual(policy.shot_durations(5.0), [5.0])

    def test_long_scene_splits_into_multiple_shots_summing_to_total(self):
        durations = policy.shot_durations(12.0)
        self.assertGreater(len(durations), 1)
        self.assertAlmostEqual(sum(durations), 12.0)
        self.assertTrue(all(d >= policy.BROLL_MIN_SHOT_SEC - 1e-6 for d in durations))

    def test_shot_queries_uses_planned_shots_before_falling_back(self):
        scene = {
            "visual": {"brief": "브리핑", "must_show": ["텃밭에서 상추를 뽑는 손"]},
            "shot_queries": ["woman gardening lettuce", "grandchild laughing outdoors"],
            "visual_query": "senior person daily life home",
        }
        queries = policy.shot_queries(scene, 3)
        self.assertEqual(queries[0], "woman gardening lettuce")
        self.assertEqual(queries[1], "grandchild laughing outdoors")
        # Third shot exceeds the planned shot_queries, so it falls back to scene_queries().
        self.assertIn(queries[2], policy.scene_queries(scene))

    def test_shot_queries_falls_back_when_no_shot_queries_planned(self):
        scene = {"visual": {"must_show": ["텃밭에서 상추를 뽑는 손"]}, "visual_query": "senior gardening"}
        self.assertEqual(policy.shot_queries(scene, 2), ["senior gardening", "senior gardening"])

    def test_page_rotation_cycles_and_persists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broll_usage.json"
            self.assertEqual(policy.next_page_for_query("woman cooking", path=path), 1)
            policy.record_query_page("woman cooking", 1, path=path)
            self.assertEqual(policy.next_page_for_query("woman cooking", path=path), 2)

    def test_hook_fast_shots_only_applies_when_flag_and_role_both_match(self):
        hook_scene = {"role": "hook"}
        other_scene = {"role": "mechanism"}
        self.assertEqual(policy.shot_durations_for_scene(hook_scene, 10.0), policy.shot_durations(10.0))
        try:
            policy.BROLL_HOOK_FAST_SHOTS = True
            hook_shots = policy.shot_durations_for_scene(hook_scene, 10.0)
            other_shots = policy.shot_durations_for_scene(other_scene, 10.0)
            self.assertGreater(len(hook_shots), len(other_shots))
        finally:
            policy.BROLL_HOOK_FAST_SHOTS = False

    def test_recently_reused_clip_is_penalized_harder_than_an_older_one(self):
        clip = video(1, 1080, 1920)
        file_info = policy.choose_video_file(clip)
        near_score = policy.score_candidate(clip, file_info, 5, set(), [], recent_video_ids=[1])
        far_score = policy.score_candidate(clip, file_info, 5, set(), [], recent_video_ids=[99] * 30 + [1])
        self.assertLess(near_score, far_score)


if __name__ == "__main__":
    unittest.main()