import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

import content_package as cp


def write_job_files(job_dir, *, scenes=None, video_meta=None, strategy=None):
    if video_meta is not None:
        (job_dir / "video_meta.json").write_text(json.dumps(video_meta, ensure_ascii=False), encoding="utf-8")
    if scenes is not None:
        (job_dir / "scenes.json").write_text(json.dumps(scenes, ensure_ascii=False), encoding="utf-8")
    if strategy is not None:
        (job_dir / "strategy.json").write_text(json.dumps(strategy, ensure_ascii=False), encoding="utf-8")


DEFAULT_VIDEO_META = {
    "topic": "치매 예방", "main_keyword": "치매 예방", "hook_type": "반전형",
    "title": "치매 예방 습관", "core_message": "작은 습관부터 시작하세요",
    "hashtags": "#치매예방 #뇌건강",
}

DEFAULT_SCENES = [
    {"text": "훅 문장입니다", "visual_query": "senior walking"},
    {"text": "원칙 설명 문장", "visual_query": "brain scan"},
    {"text": "사례 설명 문장", "visual_query": "cooking"},
    {"text": "실천 안내 문장", "visual_query": "stretching"},
    {"text": "CTA 문장입니다", "visual_query": "smiling senior"},
]

DEFAULT_STRATEGY = {
    "cta_next": "수면과 기억력",
    "evidence_brief": [{"claim": "수면 부족은 기억력 저하와 관련있다", "source": "Sleep Journal", "year": "2023", "caveat": ""}],
}


class BuildContentPackageTests(unittest.TestCase):
    def test_missing_scenes_returns_none_and_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            write_job_files(job_dir, video_meta=DEFAULT_VIDEO_META)
            result = cp.build_content_package(job_dir)
            self.assertIsNone(result)
            self.assertFalse((job_dir / "content_package.json").exists())

    def test_full_job_produces_expected_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            write_job_files(job_dir, video_meta=DEFAULT_VIDEO_META, scenes=DEFAULT_SCENES, strategy=DEFAULT_STRATEGY)
            package = cp.build_content_package(job_dir)

            self.assertEqual(package["version"], 1)
            self.assertEqual(package["source"]["topic"], "치매 예방")
            self.assertEqual(package["hook"], "훅 문장입니다")
            self.assertEqual(package["cta"]["action"], "CTA 문장입니다")
            self.assertEqual(package["cta"]["next_topic_tease"], "수면과 기억력")
            self.assertEqual(package["hashtags"], ["#치매예방", "#뇌건강"])
            self.assertTrue(package["platforms"]["youtube_shorts"]["ready"])
            self.assertFalse(package["platforms"]["x_thread"]["ready"])
            self.assertTrue((job_dir / "content_package.json").exists())

    def test_scene_roles_are_hook_body_cta(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            write_job_files(job_dir, video_meta=DEFAULT_VIDEO_META, scenes=DEFAULT_SCENES, strategy=DEFAULT_STRATEGY)
            package = cp.build_content_package(job_dir)
            roles = [s["role"] for s in package["scenes"]]
            self.assertEqual(roles[0], "hook")
            self.assertEqual(roles[-1], "cta")
            self.assertTrue(all(r in ("principle", "example") for r in roles[1:-1]))

    def test_key_points_between_three_and_seven(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            many_scenes = [DEFAULT_SCENES[0]] + [{"text": f"본문 {i}", "visual_query": "x"} for i in range(10)] + [DEFAULT_SCENES[-1]]
            write_job_files(job_dir, video_meta=DEFAULT_VIDEO_META, scenes=many_scenes, strategy=DEFAULT_STRATEGY)
            package = cp.build_content_package(job_dir)
            self.assertLessEqual(len(package["key_points"]), cp.MAX_KEY_POINTS)
            self.assertGreaterEqual(len(package["key_points"]), cp.MIN_KEY_POINTS)

    def test_evidence_comes_from_strategy_evidence_brief_no_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            write_job_files(job_dir, video_meta=DEFAULT_VIDEO_META, scenes=DEFAULT_SCENES, strategy=DEFAULT_STRATEGY)
            package = cp.build_content_package(job_dir)
            self.assertEqual(len(package["evidence"]), 1)
            self.assertEqual(package["evidence"][0]["source_hint"], "Sleep Journal")

    def test_missing_strategy_degrades_to_empty_evidence_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            write_job_files(job_dir, video_meta=DEFAULT_VIDEO_META, scenes=DEFAULT_SCENES)
            package = cp.build_content_package(job_dir)
            self.assertEqual(package["evidence"], [])

    def test_created_at_is_stable_across_rebuilds_but_updated_at_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            write_job_files(job_dir, video_meta=DEFAULT_VIDEO_META, scenes=DEFAULT_SCENES, strategy=DEFAULT_STRATEGY)
            first = cp.build_content_package(job_dir)
            second = cp.build_content_package(job_dir)
            self.assertEqual(first["created_at"], second["created_at"])

    def test_platform_ready_flags_survive_a_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            write_job_files(job_dir, video_meta=DEFAULT_VIDEO_META, scenes=DEFAULT_SCENES, strategy=DEFAULT_STRATEGY)
            cp.build_content_package(job_dir)
            cp.update_platform_flag(job_dir, "x_thread", True)
            rebuilt = cp.build_content_package(job_dir)
            self.assertTrue(rebuilt["platforms"]["x_thread"]["ready"])


class LoadAndUpdatePlatformFlagTests(unittest.TestCase):
    def test_load_missing_package_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(cp.load_content_package(tmp))

    def test_update_platform_flag_on_missing_package_is_a_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(cp.update_platform_flag(tmp, "x_thread", True))

    def test_update_platform_flag_flips_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            write_job_files(job_dir, video_meta=DEFAULT_VIDEO_META, scenes=DEFAULT_SCENES, strategy=DEFAULT_STRATEGY)
            cp.build_content_package(job_dir)
            updated = cp.update_platform_flag(job_dir, "x_thread", True)
            self.assertTrue(updated["platforms"]["x_thread"]["ready"])
            reloaded = cp.load_content_package(job_dir)
            self.assertTrue(reloaded["platforms"]["x_thread"]["ready"])


if __name__ == "__main__":
    unittest.main()
