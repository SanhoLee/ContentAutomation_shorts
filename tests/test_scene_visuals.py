"""scene_visuals: the visual plan is made from the script the operator approved.

The stage exists because `script.txt` is `scenes.json` flattened, so an edit at
the script gate used to leave the two disagreeing. These tests pin the two
properties that matters downstream: scene text always equals the approved
paragraphs, and every scene always carries a `visual_query` even when the
planning call fails (`3_broll.py` indexes it directly).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

import scene_visuals as sv
import story_types as st

SEQUENCE = st.role_sequence("myth_bust")


def scene(text, **extra):
    return {"text": text, "role": "evidence", "visual_query": "old query", **extra}


class ResyncTests(unittest.TestCase):
    def test_equal_counts_map_paragraph_to_scene_and_keep_the_rest(self):
        scenes = [scene("원래 1"), scene("원래 2")]
        resynced, changed = sv.resync_scene_text(scenes, ["고친 1", "원래 2"], SEQUENCE)
        self.assertEqual([s["text"] for s in resynced], ["고친 1", "원래 2"])
        self.assertEqual(changed, 1)
        self.assertEqual(resynced[0]["role"], "evidence")  # untouched

    def test_an_untouched_script_reports_nothing_changed(self):
        scenes = [scene("그대로 1"), scene("그대로 2")]
        _, changed = sv.resync_scene_text(scenes, ["그대로 1", "그대로 2"], SEQUENCE)
        self.assertEqual(changed, 0)

    def test_merged_paragraphs_rebuild_the_list_and_re_derive_roles(self):
        scenes = [scene("훅"), scene("본문"), scene("마무리")]
        resynced, changed = sv.resync_scene_text(scenes, ["훅 본문", "마무리"], SEQUENCE)
        self.assertEqual([s["text"] for s in resynced], ["훅 본문", "마무리"])
        self.assertEqual(changed, 2)
        # A scene whose text came from elsewhere has no claim to the old role.
        self.assertEqual(resynced[0]["role"], SEQUENCE[0])
        self.assertEqual(resynced[-1]["role"], SEQUENCE[-1])

    def test_split_paragraphs_produce_a_scene_each(self):
        resynced, _ = sv.resync_scene_text(
            [scene("훅"), scene("마무리")], ["훅", "새 문단", "마무리"], SEQUENCE,
        )
        self.assertEqual(len(resynced), 3)
        self.assertTrue(all(s["role"] in SEQUENCE for s in resynced))

    def test_an_empty_script_leaves_the_scenes_alone(self):
        # Better a stale plan than an emptied job: script.txt unreadable must
        # not wipe the scene list.
        scenes = [scene("원래 1")]
        self.assertEqual(sv.resync_scene_text(scenes, [], SEQUENCE), (scenes, 0))

    def test_blank_line_runs_from_a_hand_edit_do_not_create_empty_scenes(self):
        self.assertEqual(sv.split_script_paragraphs("가\n\n\n  \n나\n"), ["가", "나"])

    def test_an_edit_pasted_back_without_blank_lines_still_splits_per_scene(self):
        # How the real incident job came back from Slack: 8 scenes, 8 lines,
        # no blank lines. Splitting on "\n\n" would have made it one scene.
        self.assertEqual(sv.split_script_paragraphs("가\n나\n다"), ["가", "나", "다"])


class FillTests(unittest.TestCase):
    def test_a_failed_plan_still_gives_every_scene_a_searchable_query(self):
        filled, missing = sv.fill_scene_visuals([scene("장면 하나"), scene("장면 둘")], None, SEQUENCE)
        for s in filled:
            self.assertEqual(s["visual_query"], st.FALLBACK_VISUAL_QUERY)
            self.assertTrue(s["visual"]["brief"].strip())
            self.assertIn(s["visual"]["type"], st.VISUAL_TYPES)
        self.assertEqual(missing, 2)

    def test_a_planned_scene_is_kept_verbatim(self):
        planned = [{"visual": {"type": "proof", "brief": "약통을 여는 손", "must_show": ["약통"]},
                    "visual_query": "senior hands pill box"}]
        filled, missing = sv.fill_scene_visuals([scene("장면")], planned, SEQUENCE)
        self.assertEqual(filled[0]["visual"], {"type": "proof", "brief": "약통을 여는 손", "must_show": ["약통"]})
        self.assertEqual(filled[0]["visual_query"], "senior hands pill box")
        self.assertEqual(missing, 0)

    def test_an_invalid_visual_type_falls_back_to_the_role_default(self):
        planned = [{"visual": {"type": "존재하지않음", "brief": "훅 화면"}, "visual_query": "q"}]
        filled, _ = sv.fill_scene_visuals([scene("장면")], planned, SEQUENCE)
        self.assertIn(filled[0]["visual"]["type"], st.VISUAL_TYPES)

    def test_must_show_is_coerced_to_a_capped_list(self):
        planned = [
            {"visual": {"brief": "화면", "must_show": "단일 문자열"}},
            {"visual": {"brief": "화면", "must_show": ["1", "2", "3", "4", "5"]}},
        ]
        filled, _ = sv.fill_scene_visuals([scene("가"), scene("나")], planned, SEQUENCE)
        self.assertEqual(filled[0]["visual"]["must_show"], ["단일 문자열"])
        self.assertEqual(len(filled[1]["visual"]["must_show"]), 3)

    def test_a_role_outside_the_genre_sequence_is_replaced_positionally(self):
        filled, _ = sv.fill_scene_visuals([{"text": "가", "role": "설명"}], None, SEQUENCE)
        self.assertEqual(filled[0]["role"], SEQUENCE[0])


class RefreshTests(unittest.TestCase):
    def build_job(self, tmp, script_text, scenes):
        job_dir = Path(tmp)
        (job_dir / "scenes.json").write_text(json.dumps(scenes, ensure_ascii=False), encoding="utf-8")
        (job_dir / "script.txt").write_text(script_text, encoding="utf-8")
        (job_dir / "video_meta.json").write_text(json.dumps({
            "topic": "수면", "main_keyword": "수면", "hook_type": "반전형",
            "title": "수면이 기억을 정리하는 법", "core_message": "메시지",
            "hashtags": "#수면", "story_type": "myth_bust",
        }, ensure_ascii=False), encoding="utf-8")
        (job_dir / "strategy.json").write_text(json.dumps({"story_type": "myth_bust"}), encoding="utf-8")
        return job_dir

    def refresh(self, job_dir, planned=None):
        with mock.patch.object(sv, "plan_with_claude", return_value=planned):
            return sv.refresh(job_dir)

    def test_an_edited_body_reaches_scenes_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = self.build_job(
                tmp, "고친 훅\n\n고친 본문\n\n고친 마무리",
                [scene("낡은 훅"), scene("낡은 본문"), scene("낡은 마무리")],
            )
            result = self.refresh(job_dir)
            written = json.loads((job_dir / "scenes.json").read_text(encoding="utf-8"))
            self.assertEqual([s["text"] for s in written], ["고친 훅", "고친 본문", "고친 마무리"])
            self.assertEqual(result["resynced"], 3)
            self.assertTrue(all(s["visual_query"] for s in written))

    def test_a_failed_plan_is_a_warning_not_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = self.build_job(tmp, "훅\n\n마무리", [scene("훅"), scene("마무리")])
            self.assertIsNotNone(self.refresh(job_dir))
            quality = json.loads((job_dir / "script_quality.json").read_text(encoding="utf-8"))
            codes = {w["code"] for w in quality["warnings"]}
            self.assertIn("visual_plan_unavailable", codes)
            self.assertIn("visual_brief_backfilled", codes)
            self.assertEqual(quality["metrics"]["visual_plan_source"], "fallback")
            self.assertEqual(quality["metrics"]["scene_count"], 2)

    def test_the_content_package_is_rebuilt_and_keeps_created_at(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = self.build_job(tmp, "고친 훅\n\n마무리", [scene("낡은 훅"), scene("마무리")])
            (job_dir / "content_package.json").write_text(json.dumps({
                "created_at": "2020-01-01T00:00:00+00:00",
                "platforms": {"x": {"ready": True}},
                "hook": "낡은 훅",
            }, ensure_ascii=False), encoding="utf-8")
            self.refresh(job_dir)
            package = json.loads((job_dir / "content_package.json").read_text(encoding="utf-8"))
            self.assertEqual(package["hook"], "고친 훅")
            self.assertEqual(package["created_at"], "2020-01-01T00:00:00+00:00")
            self.assertEqual(package["platforms"], {"x": {"ready": True}})

    def test_a_job_without_scenes_is_skipped_rather_than_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(sv.refresh(Path(tmp)))


class PlanCallTests(unittest.TestCase):
    def test_a_response_of_the_wrong_length_is_rejected(self):
        # A shifted plan is worse than none: the deterministic fill at least
        # stays in step with the text.
        body = {"content": [{"type": "text", "text": json.dumps({"scenes": [{"index": 1}]})}]}
        with mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "k"}), \
                mock.patch.object(sv.claude_cost, "assert_budget"), \
                mock.patch("requests.post") as post:
            post.return_value = mock.Mock(raise_for_status=mock.Mock(), json=lambda: body)
            self.assertIsNone(sv.plan_with_claude([scene("가"), scene("나")], topic="수면", title="제목"))

    def test_no_api_key_means_no_call(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch("requests.post") as post:
            self.assertIsNone(sv.plan_with_claude([scene("가")], topic="수면", title="제목"))
            post.assert_not_called()

    def test_the_prompt_bans_the_query_terms_that_returned_static_footage(self):
        prompt = sv.build_plan_prompt([scene("가")], topic="수면", title="제목")
        for banned in ("slow motion", "timelapse", "cinematic", "aerial", "background"):
            self.assertIn(banned, prompt)
        self.assertIn("2개", prompt)  # the "generic + 1-2 topic words" rule


if __name__ == "__main__":
    unittest.main()
