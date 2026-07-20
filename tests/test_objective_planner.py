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

    def test_validators_reject_refs_when_no_evidence_was_supplied(self):
        candidates = [{"candidate_id": "cand_01", "topic": "수면 신호", "evidence_refs": []}]
        with self.assertRaises(objective_planner.PlannerValidationError):
            objective_planner.validate_planner_output(
                {"candidates": [{
                    "candidate_id": "cand_01", "topic": "새 수면 각도",
                    "evidence_refs": ["video:v1"],
                }]},
                candidates,
                valid_refs=set(),
                existing_video_ids=set(),
            )
        with self.assertRaises(objective_planner.PlannerValidationError):
            objective_planner.validate_critic_output(
                {"reviews": [{
                    "candidate_id": "cand_01",
                    "contradicting_refs": ["video:v1"],
                }]},
                {"cand_01"},
                valid_refs=set(),
            )

    def test_low_score_and_low_confidence_require_manual_review(self):
        judgment = objective_planner.judge_candidate(
            {"confidence": 0.0, "duplicate_similarity": 1.0},
            {"base_score": 20.0},
            None,
            desired_exploration="exploit",
        )
        self.assertEqual(judgment["decision"], "manual_review")

    def test_trend_candidates_do_not_inherit_unrelated_video_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = objective_planner.feedback.connect(Path(tmp) / "feedback.db")
            with conn:
                objective_planner.feedback.store_videos(conn, [{
                    "video_id": "v1",
                    "title": "수면 부족과 기억력",
                    "published_at": "2026-06-01T00:00:00Z",
                    "duration_seconds": 60,
                    "fetched_at": "2026-07-01T00:00:00+00:00",
                }])
                objective_planner.feedback.store_performance_snapshot(conn, {
                    "video_id": "v1",
                    "window_name": "D28",
                    "period_start": "2026-06-01",
                    "period_end": "2026-06-28",
                    "elapsed_days": 28,
                    "views": 1000,
                    "engaged_views": 700,
                    "average_view_percentage": 80,
                    "fetched_at": "2026-07-01T00:00:00+00:00",
                })
            candidates = objective_planner.build_candidate_pool(
                conn,
                objective_type="reach",
                trend_candidates=["새 트렌드 A", "새 트렌드 B", "새 트렌드 C"],
            )
            conn.close()

        trend_candidates = [item for item in candidates if item["candidate_source"] == "trend"]
        self.assertEqual(len(trend_candidates), 3)
        self.assertTrue(all(not item["evidence_refs"] for item in trend_candidates))
        self.assertTrue(all(item["source_classification"] == "insufficient_data" for item in trend_candidates))

    def test_deterministic_fallback_writes_compatible_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "topic_plan.json"
            plan = objective_planner.plan_objective_topic(
                "subscriber_growth", seed_topic="수면", job_id="fixed_job",
                output_path=output, db_path=Path(tmp) / "feedback.db", allow_ai=False,
            )
            self.assertEqual(plan["objective"]["decision"], "manual_review")
            self.assertEqual(plan["strategy_source"], "deterministic_fallback")
            self.assertIn("content_design", plan)
            self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
