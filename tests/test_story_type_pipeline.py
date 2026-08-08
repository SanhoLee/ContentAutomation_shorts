"""Story types as they actually reach the pipeline: Stage 1's contract, Stage 2's
prompt and scenes, the package, and the B-roll query.

USE_STORY_TYPES is read into a module global at import time (like every other
runtime knob in 0_script.py), so the on/off variants are separate module loads
rather than a monkeypatched flag.
"""

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    class _RequestsStub:
        def post(self, *args, **kwargs):
            raise RuntimeError("requests is not installed")
    sys.modules["requests"] = _RequestsStub()

REPO_ROOT = Path(__file__).resolve().parents[1]
DEV_SRC = REPO_ROOT / "dev" / "src"
os.environ.setdefault("WORK_DIR", tempfile.mkdtemp(prefix="story_type_pipeline_"))
sys.path.insert(0, str(DEV_SRC / "common"))
sys.path.insert(0, str(DEV_SRC / "youtube"))

import broll_policy
import content_package
import story_types as st


def load_script_module(**env):
    """A fresh 0_script module with `env` applied, restoring the environment."""
    previous = {key: os.environ.get(key) for key in env}
    os.environ.update({key: str(value) for key, value in env.items()})
    try:
        spec = importlib.util.spec_from_file_location(
            f"script0_{'_'.join(env.values()) or 'default'}", DEV_SRC / "common" / "0_script.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


SCRIPT_ON = load_script_module(USE_STORY_TYPES="1")
SCRIPT_OFF = load_script_module(USE_STORY_TYPES="0")

STRATEGY = {
    "topic": "수면과 기억력",
    "main_keyword": "수면 기억력",
    "title": "수면이 기억을 정리하는 법",
    "hook_type": "반전형",
    "core_message": "잠은 기억을 지웁니다",
    "cta_next": "다음 습관",
    "story_type": "myth_bust",
}


def scenes(count=5):
    return [
        {"text": f"장면 {index} 문장입니다", "visual_query": f"senior walking {index}"}
        for index in range(count)
    ]


class Stage1ContractTests(unittest.TestCase):
    def test_schema_requires_story_type_from_the_four_ids(self):
        schema = SCRIPT_ON.strategy_output_schema()
        self.assertIn("story_type", schema["required"])
        self.assertEqual(schema["properties"]["story_type"]["enum"], list(st.STORY_TYPES))

    def test_a_decided_story_type_is_marked_unchangeable(self):
        directive = SCRIPT_ON.story_type_directive("myth_bust", decided=True)
        self.assertIn("변경 금지", directive)
        self.assertIn("myth_bust", directive)

    def test_an_undecided_job_is_offered_all_four(self):
        directive = SCRIPT_ON.story_type_directive(None, decided=False)
        for story_type in st.STORY_TYPES:
            self.assertIn(story_type, directive)

    def test_planner_design_is_read_as_the_decided_story_type(self):
        strategy = {"content_design": {"story_type": "case_journey"}}
        self.assertEqual(SCRIPT_ON.strategy_story_type(strategy), "case_journey")

    def test_a_legacy_strategy_without_story_type_derives_one_from_its_format(self):
        self.assertEqual(
            SCRIPT_ON.strategy_story_type({"content_design": {"format_type": "행동챌린지형"}}),
            "habit_mechanism",
        )

    def test_a_job_with_no_genre_at_all_reports_none(self):
        self.assertIsNone(SCRIPT_ON.strategy_story_type({"topic": "수면"}))

    def test_design_constraint_hint_carries_the_story_type(self):
        hint = SCRIPT_ON.design_constraint_hint({"story_type": "habit_mechanism"})
        self.assertIn("habit_mechanism", hint)


class ApplyStoryTypeTests(unittest.TestCase):
    def test_conflicting_format_is_rewritten_to_match_the_story_type(self):
        strategy = SCRIPT_ON.apply_story_type({"story_type": "habit_mechanism", "format_type": "사례추적형"})
        self.assertEqual(strategy["story_type"], "habit_mechanism")
        self.assertEqual(strategy["format_type"], "행동챌린지형")

    def test_content_design_is_kept_in_step(self):
        strategy = SCRIPT_ON.apply_story_type({
            "story_type": "myth_bust", "content_design": {"format_type": "사례추적형"},
        })
        self.assertEqual(strategy["content_design"]["story_type"], "myth_bust")
        self.assertEqual(strategy["content_design"]["format_type"], "오해반전형")

    def test_a_genreless_strategy_gets_the_configured_default(self):
        strategy = SCRIPT_ON.apply_story_type({"topic": "수면"})
        self.assertEqual(strategy["story_type"], st.load_config()["default_story_type"])

    def test_normalize_strategy_contract_settles_the_genre(self):
        strategy = SCRIPT_ON.normalize_strategy_contract({"format_type": "연구발견형"}, "수면과 기억")
        self.assertEqual(strategy["story_type"], "principle_experience")

    def test_off_leaves_the_strategy_untouched(self):
        strategy = SCRIPT_OFF.apply_story_type({"format_type": "사례추적형"})
        self.assertNotIn("story_type", strategy)


class PlannerContractSurvivalTests(unittest.TestCase):
    def test_planner_story_type_outranks_stage_one(self):
        merged = SCRIPT_ON.merge_planning_contract(
            {"story_type": "habit_mechanism", "title": "제목"},
            {"content_design": {"story_type": "case_journey", "format_type": "사례추적형"}},
        )
        self.assertEqual(merged["story_type"], "case_journey")

    def test_stage_one_choice_is_kept_when_planning_had_none(self):
        merged = SCRIPT_ON.merge_planning_contract(
            {"story_type": "habit_mechanism"}, {"content_design": {"angle": "생활"}},
        )
        self.assertEqual(merged["story_type"], "habit_mechanism")


class Stage2PromptTests(unittest.TestCase):
    def test_prompt_carries_the_genre_skeleton_and_role_sequence(self):
        prompt = SCRIPT_ON.build_prompt(dict(STRATEGY), abstracts="")
        self.assertIn("myth_bust", prompt)
        self.assertIn("why_believed", prompt)

    def test_the_prompt_no_longer_asks_stage_two_to_plan_visuals(self):
        # The visual plan is scene_visuals' job now: asking here would fix it
        # before the operator is allowed to edit the words it describes.
        for module in (SCRIPT_ON, SCRIPT_OFF):
            prompt = module.build_prompt(dict(STRATEGY), abstracts="")
            self.assertNotIn('"visual_query"', prompt)
            self.assertNotIn('"must_show"', prompt)

    def test_each_genre_injects_its_own_skeleton(self):
        for story_type in st.STORY_TYPES:
            prompt = SCRIPT_ON.build_prompt({**STRATEGY, "story_type": story_type}, abstracts="")
            for role in st.role_sequence(story_type):
                self.assertIn(role, prompt, f"{story_type}: {role} 누락")

    def test_off_restores_the_pre_story_type_prompt(self):
        prompt = SCRIPT_OFF.build_prompt(dict(STRATEGY), abstracts="")
        self.assertNotIn("must_show", prompt)
        self.assertNotIn("스토리 타입", prompt)
        self.assertNotIn('"role"', prompt)

    def test_off_schema_has_no_story_type(self):
        self.assertNotIn("story_type", SCRIPT_OFF.strategy_output_schema()["properties"])

    def test_the_scene_shape_is_valid_json_in_both_modes(self):
        """The shape is interpolated into the prompt f-string as a value, so its
        braces must be single — doubling them shipped a literal `{{` to Claude."""
        for module in (SCRIPT_ON, SCRIPT_OFF):
            _, shape = module.scene_output_spec()
            self.assertNotIn("{{", shape)
            parsed = json.loads(shape)
            self.assertIn("text", parsed)
            self.assertNotIn("visual_query", parsed)
        self.assertIn("role", json.loads(SCRIPT_ON.scene_output_spec()[1]))
        self.assertEqual(list(json.loads(SCRIPT_OFF.scene_output_spec()[1])), ["text"])

    def test_the_rendered_prompt_shows_a_parseable_scene_example(self):
        for module in (SCRIPT_ON, SCRIPT_OFF):
            prompt = module.build_prompt(dict(STRATEGY), abstracts="")
            example = prompt.split('"scenes": [')[1].split("\n")[1].strip()
            self.assertIn("text", json.loads(example))

    def test_missing_template_directory_degrades_to_no_block(self):
        previous = os.environ.get("STORY_TEMPLATES_DIR")
        os.environ["STORY_TEMPLATES_DIR"] = "/no/such/templates"
        try:
            self.assertEqual(SCRIPT_ON.story_type_block(dict(STRATEGY)), "")
        finally:
            if previous is None:
                os.environ.pop("STORY_TEMPLATES_DIR", None)
            else:
                os.environ["STORY_TEMPLATES_DIR"] = previous


class NormalizeScenesTests(unittest.TestCase):
    def normalized(self, raw, strategy=None):
        result = SCRIPT_ON.normalize_story_scenes({"scenes": raw}, strategy or dict(STRATEGY))
        return result["scenes"]

    def test_roles_follow_the_genre_sequence_and_bookend_correctly(self):
        result = self.normalized(scenes(7))
        sequence = st.role_sequence("myth_bust")
        self.assertEqual(result[0]["role"], sequence[0])
        self.assertEqual(result[-1]["role"], sequence[-1])
        for scene in result:
            self.assertIn(scene["role"], sequence)

    def test_more_scenes_than_roles_repeats_middles_rather_than_dropping_any(self):
        result = self.normalized(scenes(12))
        self.assertEqual(result[0]["role"], "hook")
        self.assertEqual(result[-1]["role"], "cta")
        self.assertEqual(len(result), 12)

    def test_a_single_scene_script_still_normalizes(self):
        result = self.normalized(scenes(1))
        self.assertEqual(result[0]["role"], "hook")

    def test_a_model_supplied_role_outside_the_sequence_is_replaced(self):
        raw = scenes(3)
        raw[1]["role"] = "설명"
        self.assertIn(self.normalized(raw)[1]["role"], st.role_sequence("myth_bust"))

    def test_the_visual_plan_is_left_to_the_scene_visuals_stage(self):
        # Stage 2 now only settles the narrative beat; visual/visual_query are
        # filled after the script gate (see tests/test_scene_visuals.py).
        for scene in self.normalized(scenes(4)):
            self.assertNotIn("visual", scene)

    def test_empty_scenes_are_left_alone(self):
        self.assertEqual(SCRIPT_ON.normalize_story_scenes({"scenes": []}, dict(STRATEGY))["scenes"], [])

    def test_off_leaves_scenes_exactly_as_the_model_wrote_them(self):
        raw = scenes(3)
        result = SCRIPT_OFF.normalize_story_scenes({"scenes": raw}, dict(STRATEGY))
        self.assertEqual(result["scenes"], raw)


class QualityReportTests(unittest.TestCase):
    def build_result(self, scene_list):
        return {
            "scenes": scene_list, "final_answer": "잠은 기억을 정리합니다",
            "hook_open_loop": "그런데 진짜 이유는 따로 있습니다", "promise_fulfilled": True,
        }

    def test_story_type_is_recorded_in_the_metrics(self):
        result = SCRIPT_ON.normalize_story_scenes(self.build_result(scenes(6)), dict(STRATEGY))
        report = SCRIPT_ON.validate_script(result, dict(STRATEGY))
        self.assertEqual(report["metrics"]["story_type"], "myth_bust")
        # scenes_with_visual_brief is scene_visuals' metric now; Stage 2 has no
        # visual plan to count and must not claim one.
        self.assertNotIn("scenes_with_visual_brief", report["metrics"])

    def test_wrong_bookend_roles_are_reported(self):
        raw = scenes(4)
        for scene in raw:
            scene["role"] = "evidence"
        report = SCRIPT_ON.validate_script(self.build_result(raw), dict(STRATEGY))
        codes = {issue["code"] for issue in report["warnings"]}
        self.assertIn("role_bookends_missing", codes)
        self.assertTrue(report["ok"])


class ContentPackageTests(unittest.TestCase):
    def build(self, job_dir, scene_list, meta_extra=None):
        (job_dir / "video_meta.json").write_text(json.dumps({
            "topic": "수면", "main_keyword": "수면", "hook_type": "반전형",
            "title": "수면", "core_message": "메시지", "hashtags": "#수면",
            **(meta_extra or {}),
        }, ensure_ascii=False), encoding="utf-8")
        (job_dir / "scenes.json").write_text(json.dumps(scene_list, ensure_ascii=False), encoding="utf-8")
        return content_package.build_content_package(job_dir)

    def test_story_type_and_visual_reach_the_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            scene_list = [
                {"text": "훅", "role": "hook", "visual_query": "a",
                 "visual": {"type": "paradox", "brief": "훅 화면", "must_show": ["시계"]}},
                {"text": "근거", "role": "evidence", "visual_query": "b",
                 "visual": {"type": "proof", "brief": "근거 화면", "must_show": []}},
                {"text": "마무리", "role": "cta", "visual_query": "c",
                 "visual": {"type": "broll", "brief": "마무리 화면", "must_show": []}},
            ]
            package = self.build(
                Path(tmp), scene_list,
                {"story_type": "myth_bust", "format_type": "오해반전형"},
            )
            self.assertEqual(package["story_type"], "myth_bust")
            self.assertEqual(package["format_type"], "오해반전형")
            self.assertEqual(package["scenes"][0]["role"], "hook")
            self.assertEqual(package["scenes"][1]["visual"]["brief"], "근거 화면")

    def test_a_pre_story_type_job_still_packages_with_the_position_heuristic(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = self.build(Path(tmp), [
                {"text": "훅", "visual_query": "a"},
                {"text": "본문", "visual_query": "b"},
                {"text": "마무리", "visual_query": "c"},
            ])
            self.assertEqual(package["story_type"], "")
            self.assertEqual([s["role"] for s in package["scenes"]], ["hook", "principle", "cta"])
            self.assertNotIn("visual", package["scenes"][0])


class BrollQueryTests(unittest.TestCase):
    def test_english_brief_outranks_the_visual_query(self):
        scene = {
            "visual_query": "senior walking",
            "visual": {"brief": "hands opening pill box", "must_show": ["pill box"]},
        }
        self.assertEqual(broll_policy.scene_queries(scene)[0], "hands opening pill box")

    def test_a_korean_brief_is_skipped_because_pexels_is_english_indexed(self):
        scene = {
            "visual_query": "senior walking outdoors",
            "visual": {"brief": "약통을 여는 손", "must_show": ["약통"]},
        }
        self.assertEqual(broll_policy.scene_queries(scene), ["senior walking outdoors"])

    def test_must_show_is_used_when_the_brief_is_unusable(self):
        scene = {
            "visual_query": "senior walking",
            "visual": {"brief": "약통을 여는 손", "must_show": ["pill", "box"]},
        }
        self.assertEqual(broll_policy.scene_queries(scene), ["pill box", "senior walking"])

    def test_a_scene_without_any_visual_block_still_yields_its_query(self):
        self.assertEqual(broll_policy.scene_queries({"visual_query": "senior walking"}), ["senior walking"])

    def test_duplicate_candidates_are_not_searched_twice(self):
        scene = {"visual_query": "senior walking", "visual": {"brief": "senior walking", "must_show": []}}
        self.assertEqual(broll_policy.scene_queries(scene), ["senior walking"])

    def test_a_fully_korean_scene_yields_nothing_searchable(self):
        scene = {"visual_query": "", "visual": {"brief": "약통", "must_show": ["약통"]}}
        self.assertEqual(broll_policy.scene_queries(scene), [])


if __name__ == "__main__":
    unittest.main()
