import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src"))

import objective_planner


class ObjectivePlannerTests(unittest.TestCase):
    def test_objective_changes_candidate_ranking(self):
        subscriber_candidate = {
            "normalized_metrics": {
                "net_subscriber_conversion": 1, "subscriber_conversion": 1,
                "average_view_percentage": 0.5, "initial_engagement": 0.5,
                "share_rate": 0.5, "comment_rate": 0.5,
                "trend_signal": 0.2, "novelty": 0.2,
            }
        }
        reach_candidate = {
            "normalized_metrics": {
                "views_percentile": 1, "initial_engagement": 1,
                "average_view_percentage": 0.5, "trend_signal": 1,
                "share_rate": 0.5, "novelty": 1,
                "net_subscriber_conversion": 0.1, "subscriber_conversion": 0.1,
            }
        }
        planner = {field: "medium" for field in objective_planner.ENUM_FIELDS}
        self.assertGreater(
            objective_planner.score_candidate(subscriber_candidate, planner, "subscriber_growth")["base_score"],
            objective_planner.score_candidate(reach_candidate, planner, "subscriber_growth")["base_score"],
        )
        self.assertGreater(
            objective_planner.score_candidate(reach_candidate, planner, "reach")["base_score"],
            objective_planner.score_candidate(subscriber_candidate, planner, "reach")["base_score"],
        )

    def test_planner_validator_rejects_unknown_refs_and_ignores_numeric_scores(self):
        candidates = [{"candidate_id": "cand_01", "topic": "수면 신호", "evidence_refs": ["video:v1"]}]
        with self.assertRaises(objective_planner.PlannerValidationError):
            objective_planner.validate_planner_output(
                {"candidates": [{
                    "candidate_id": "cand_01", "topic": "새 수면 각도",
                    "series_potential": "high", "evidence_refs": ["video:made_up"],
                }]}, candidates, valid_refs={"video:v1"}, existing_video_ids={"v1"},
            )
        valid = objective_planner.validate_planner_output(
            {"candidates": [{
                "candidate_id": "cand_01", "topic": "새 수면 각도",
                "series_potential": "high", "evidence_refs": ["video:v1"], "score": 999,
            }]}, candidates, valid_refs={"video:v1"}, existing_video_ids={"v1"},
        )
        self.assertNotIn("score", valid["candidates"][0])

    def test_deterministic_fallback_writes_compatible_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "topic_plan.json"
            plan = objective_planner.plan_objective_topic(
                "subscriber_growth", seed_topic="수면", job_id="fixed_job",
                output_path=output, db_path=Path(tmp) / "feedback.db", allow_ai=False,
            )
            self.assertEqual(plan["objective"]["decision"], "limited_test")
            self.assertEqual(plan["strategy_source"], "deterministic_fallback")
            self.assertIn("content_design", plan)
            self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
