import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dev" / "src"
sys.path.insert(0, str(SRC))


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SRC / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script0 = load_module("goal_script0", "0_script.py")
upload = load_module("goal_upload", "4_upload.py")


class GoalPipelineTests(unittest.TestCase):
    def test_topic_plan_objective_and_design_reach_prompt_and_meta(self):
        strategy = script0.normalize_strategy_contract({
            "topic": "새벽 수면", "main_keyword": "새벽 수면", "title": "잠이 끊기는 이유",
            "hook_type": "반전형", "core_message": "잠의 연속성을 살펴보세요",
            "objective": {"type": "retention", "plan_id": 7, "decision": "limited_test", "confidence": 0.5},
            "content_design": {
                "topic_family": "수면", "format_type": "자가진단형",
                "emotion_curve": ["공감", "점검", "안심", "행동"], "series_key": "수면 신호",
            },
        }, "새벽 수면")
        prompt = script0.build_prompt(strategy, "")
        self.assertIn("retention", prompt)
        self.assertIn("자가진단형", prompt)
        self.assertIn("공감 → 점검 → 안심 → 행동", prompt)

        result = {
            "title": "잠이 끊기는 이유", "hook_type": "반전형", "summary": "요약",
            "hashtags": "#수면", "description": "설명", "final_answer": "수면 신호를 살펴보세요.",
            "promise_fulfilled": True, "evidence_limit": "개인차", "thumbnail_text": ["수면 신호"],
            "frame_header": {"title": "수면신호", "subtitle": "잠이 끊기는 이유"},
            "scenes": [{"text": "수면 신호를 살펴보세요", "visual_query": "senior sleep"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            old = script0.WORK_DIR
            try:
                script0.WORK_DIR = tmp
                script0.write_outputs(result, strategy)
                meta = json.loads(Path(tmp, "video_meta.json").read_text(encoding="utf-8"))
                self.assertEqual(meta["objective"]["plan_id"], 7)
                self.assertEqual(meta["content_design"]["format_type"], "자가진단형")
            finally:
                script0.WORK_DIR = old

    def test_upload_result_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = upload.write_upload_result(
                job_id="auto_1", video_id="video_1", uploaded_at="2026-07-20T07:30:00+09:00",
                work_dir=Path(tmp),
            )
            self.assertEqual(result["video_id"], "video_1")
            self.assertEqual(
                json.loads(Path(tmp, "upload_result.json").read_text(encoding="utf-8")), result
            )


if __name__ == "__main__":
    unittest.main()
