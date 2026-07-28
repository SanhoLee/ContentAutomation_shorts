import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

import objective_planner


class ObjectivePlannerTests(unittest.TestCase):
    def test_objective_changes_candidate_ranking(self):
        # channel_reliability is explicit here because build_candidate_pool
        # always populates it from evidence_profile; omitting it makes
        # score_candidate treat the channel as brand-new (reliability=0),
        # which maxes out trend_novelty_weight (0.25) and lets an extreme
        # trend_signal/novelty=1 candidate override a much stronger metric
        # fit. A reliability of 1.0 reflects a channel with an established
        # performance history, which is the realistic case this test is
        # trying to check ("does the objective's metric weighting matter").
        subscriber_candidate = {
            "channel_reliability": 1.0,
            "normalized_metrics": {
                "net_subscriber_conversion": 1, "subscriber_conversion": 1,
                "average_view_percentage": 0.5, "initial_engagement": 0.5,
                "share_rate": 0.5, "comment_rate": 0.5,
                "trend_signal": 0.2, "novelty": 0.2, "research_depth": 0.5,
            }
        }
        reach_candidate = {
            "channel_reliability": 1.0,
            "normalized_metrics": {
                "views_percentile": 1, "initial_engagement": 1,
                "average_view_percentage": 0.5, "trend_signal": 1,
                "share_rate": 0.5, "novelty": 1,
                "net_subscriber_conversion": 0.1, "subscriber_conversion": 0.1,
                "research_depth": 0.5,
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

    def test_duplicate_gate_blocks_prefixed_copy_and_pool_does_not_reuse_old_title(self):
        old_title = "냉동 블루베리 안토시아닌, 신선한 것보다 좋다"
        duplicate = objective_planner._topic_duplicate_info(
            f"보조 식품: {old_title} - 놓치기 쉬운 신호",
            [old_title],
            0.25,
        )
        self.assertTrue(duplicate["blocked"])
        self.assertGreaterEqual(duplicate["containment"], 0.8)

        with tempfile.TemporaryDirectory() as tmp:
            conn = objective_planner.feedback.connect(Path(tmp) / "feedback.db")
            with conn:
                objective_planner.feedback.store_videos(conn, [{
                    "video_id": "blueberry",
                    "title": old_title,
                    "published_at": "2026-06-01T00:00:00Z",
                    "duration_seconds": 60,
                    "fetched_at": "2026-07-01T00:00:00+00:00",
                }])
            candidates = objective_planner.build_candidate_pool(
                conn, objective_type="reach", seed_topic="보조 식품",
            )
            conn.close()
        self.assertTrue(candidates)
        self.assertTrue(all(old_title not in item["topic"] for item in candidates))

    def test_truncated_claude_json_is_rejected_before_validation(self):
        with self.assertRaisesRegex(objective_planner.PlannerValidationError, "토큰 한도"):
            objective_planner._extract_claude_json({
                "stop_reason": "max_tokens",
                "content": [{"type": "text", "text": '{"candidates": ['}],
            })

    def test_call_claude_json_retries_once_with_more_tokens_on_truncation(self):
        responses = [
            {"stop_reason": "max_tokens", "content": [{"type": "text", "text": '{"candidates": ['}]},
            {"stop_reason": "end_turn", "content": [{"type": "text", "text": '{"ok": true}'}]},
        ]
        sent_max_tokens = []

        class FakeResponse:
            def __init__(self, payload):
                self.status_code = 200
                self.headers = {}
                self._payload = payload

            def json(self):
                return dict(self._payload)

            def raise_for_status(self):
                return None

        def fake_post(_url, headers=None, json=None, timeout=None):
            sent_max_tokens.append(json["max_tokens"])
            return FakeResponse(responses[len(sent_max_tokens) - 1])

        env = {"ANTHROPIC_API_KEY": "test-key"}
        with tempfile.TemporaryDirectory() as tmp:
            env["WORK_DIR"] = tmp
            with unittest.mock.patch.dict(os.environ, env), \
                    unittest.mock.patch("requests.post", side_effect=fake_post):
                result = objective_planner.call_claude_json(
                    "prompt", model="claude-haiku-4-5-20251001", max_tokens=100,
                    stage="candidate_planner", job_id="retry_test",
                )

        self.assertEqual(result, {"ok": True})
        self.assertEqual(sent_max_tokens, [100, 150])

    def test_manual_planning_history_is_part_of_duplicate_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "feedback.db"
            previous = objective_planner.plan_objective_topic(
                "reach", seed_topic="마그네슘", job_id="previous_manual",
                db_path=db_path, allow_ai=False,
            )
            conn = objective_planner.feedback.connect(db_path)
            try:
                self.assertIn(previous["topic"], objective_planner._existing_titles(conn))
                duplicate = objective_planner._topic_duplicate_info(
                    previous["topic"], objective_planner._existing_titles(conn), 0.25,
                )
            finally:
                conn.close()
        self.assertTrue(duplicate["blocked"])

    def test_planner_failure_still_runs_critic_on_fallback_candidates(self):
        calls = {"planner": 0, "critic": 0}
        original_local_rows = objective_planner._local_candidate_rows

        def ready_local_rows(*args, **kwargs):
            rows = original_local_rows(*args, **kwargs)
            rows[0]["judgment"]["adjusted_score"] = 60.0
            return rows

        def broken_planner(_prompt):
            calls["planner"] += 1
            raise objective_planner.PlannerValidationError("잘린 JSON")

        def critic(_prompt):
            calls["critic"] += 1
            return {"reviews": []}

        with tempfile.TemporaryDirectory() as tmp:
            objective_planner._local_candidate_rows = ready_local_rows
            try:
                plan = objective_planner.plan_objective_topic(
                    "reach", seed_topic="수면", job_id="planner_failure",
                    db_path=Path(tmp) / "feedback.db", planner_call=broken_planner,
                    critic_call=critic,
                )
            finally:
                objective_planner._local_candidate_rows = original_local_rows

        self.assertEqual(calls, {"planner": 1, "critic": 1})
        self.assertEqual(plan["planning"]["planner_status"], "failed")
        self.assertEqual(plan["planning"]["critic_status"], "success")

    def test_low_local_preflight_spends_no_model_calls(self):
        calls = {"planner": 0, "critic": 0}

        def planner(_prompt):
            calls["planner"] += 1
            return {"candidates": []}

        def critic(_prompt):
            calls["critic"] += 1
            return {"reviews": []}

        # Monkey-patch _local_candidate_rows to return sub-threshold scores so
        # the preflight gate fires regardless of which seed is supplied.
        # Without this, a manual seed ("수면") lowers the threshold to 20.0,
        # which the default candidate scoring already exceeds on an empty DB.
        original_local_rows = objective_planner._local_candidate_rows

        def low_score_rows(candidates, *args, **kwargs):
            rows = original_local_rows(candidates, *args, **kwargs)
            for row in rows:
                row["judgment"]["adjusted_score"] = 5.0
            return rows

        with tempfile.TemporaryDirectory() as tmp:
            objective_planner._local_candidate_rows = low_score_rows
            try:
                plan = objective_planner.plan_objective_topic(
                    "reach", seed_topic="수면", job_id="local_preflight",
                    db_path=Path(tmp) / "feedback.db", planner_call=planner,
                    critic_call=critic,
                )
            finally:
                objective_planner._local_candidate_rows = original_local_rows
        self.assertEqual(calls, {"planner": 0, "critic": 0})
        self.assertEqual(plan["planning"]["preflight_status"], "blocked")
        self.assertEqual(plan["planning"]["claude_cost_usd"], 0.0)

    def test_interpretation_replaces_keyword_family_and_template_topics(self):
        # "고독감" matches no TOPIC_FAMILY_RULES keyword, so the mechanical path
        # falls back to the seed word as its own family and bolts on
        # supplement-domain angle templates. The interpreter must override both.
        interpretation = {
            "resolved_family": "사회적고립",
            "family_source": "existing",
            "topics": {
                "exploit": ["혼자 있는 시간이 길어질 때 뇌에 생기는 변화"],
                "adjacent": ["가족과 통화 한 번이 만드는 차이"],
            },
            "evidence_relevance": {},
        }
        with tempfile.TemporaryDirectory() as tmp:
            conn = objective_planner.feedback.connect(Path(tmp) / "feedback.db")
            candidates = objective_planner.build_candidate_pool(
                conn, objective_type="reach", seed_topic="고독감",
                interpretation=interpretation,
            )
            conn.close()
        self.assertTrue(candidates)
        self.assertEqual({item["topic_family"] for item in candidates}, {"사회적고립"})
        self.assertFalse(
            any("복용 시간보다 중요한 생활 조건" in item["topic"] for item in candidates)
        )
        # Interpreted topics are finished titles used verbatim. The seed word must
        # not be prepended as a "고독감: ..." dictionary-entry prefix.
        self.assertIn("혼자 있는 시간이 길어질 때 뇌에 생기는 변화", {item["topic"] for item in candidates})
        self.assertFalse(any(item["topic"].startswith("고독감") for item in candidates))

    def test_template_fallback_still_prefixes_the_seed(self):
        # Only the fragment templates need the "<seed>: <fragment>" shape; this is
        # the no-interpretation path, so the prefix behaviour must survive there.
        with tempfile.TemporaryDirectory() as tmp:
            conn = objective_planner.feedback.connect(Path(tmp) / "feedback.db")
            candidates = objective_planner.build_candidate_pool(
                conn, objective_type="reach", seed_topic="고독감",
            )
            conn.close()
        self.assertTrue(candidates)
        self.assertTrue(all(item["topic"].startswith("고독감") for item in candidates))

    def test_confidence_is_not_discounted_a_second_time_in_the_score(self):
        # shrink_percentile and _score_blend_weights already price in sample
        # uncertainty; a third low-confidence subtraction here is what held
        # adjusted_score under the runnable threshold on a young channel.
        candidate = {"confidence": 0.2, "duplicate_similarity": 0.0, "exploration_mode": "exploit"}
        critic = {
            "duplicate_risk": "low", "overfit_risk": "low", "evidence_risk": "low",
            "recommended_action": "limited_test",
        }
        judgment = objective_planner.judge_candidate(
            candidate, {"base_score": 60.0}, critic, desired_exploration="exploit",
        )
        self.assertNotIn("low_confidence", judgment["penalties"])
        self.assertEqual(
            judgment["adjusted_score"],
            round(60.0 - judgment["penalties"]["critic_risk"], 4),
        )
        self.assertEqual(judgment["decision"], "limited_test")

    def test_off_topic_channel_evidence_is_labelled_instead_of_shown_as_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = objective_planner.feedback.connect(Path(tmp) / "feedback.db")
            with conn:
                objective_planner.feedback.store_videos(conn, [{
                    "video_id": "blueberry",
                    "title": "매일 먹어도 뇌에 안 닿는 블루베리",
                    "published_at": "2026-06-01T00:00:00Z",
                    "duration_seconds": 60,
                    "fetched_at": "2026-07-01T00:00:00+00:00",
                }])
                objective_planner.feedback.store_performance_snapshot(conn, {
                    "video_id": "blueberry",
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
                conn, objective_type="reach", seed_topic="고독감",
                interpretation={
                    "resolved_family": "사회적고립",
                    "family_source": "existing",
                    "topics": {"exploit": ["혼자 있는 시간이 길어질 때 뇌에 생기는 변화"]},
                    "evidence_relevance": {"video:blueberry": "pattern_only"},
                },
            )
            conn.close()
        with_evidence = [item for item in candidates if item["evidence_refs"]]
        self.assertTrue(with_evidence)
        for item in with_evidence:
            self.assertEqual(item["evidence_scope"], "pattern_only")
            self.assertIn("evidence_topic_mismatch", item["confounders"])
            self.assertEqual(item["evidence_titles"], ["매일 먹어도 뇌에 안 닿는 블루베리"])

    def test_seed_interpretation_validator_guards_refs_and_skips_bad_topics(self):
        with self.assertRaises(objective_planner.PlannerValidationError):
            objective_planner.validate_seed_interpretation(
                {
                    "resolved_family": "수면", "family_source": "existing",
                    "topics": {"exploit": ["밤에 자주 깨는 이유가 있습니다"]},
                    "evidence_relevance": [{"ref": "video:made_up", "relevance": "topical"}],
                },
                valid_refs={"video:v1"},
            )
        result = objective_planner.validate_seed_interpretation(
            {
                "resolved_family": "수면", "family_source": "existing",
                "topics": {"exploit": [
                    "30% 더 좋아지는 수면 습관", "밤에 자주 깨는 이유가 있습니다", "짧",
                    "기존 제목 그대로입니다",
                ]},
            },
            valid_refs={"video:v1"},
            existing_titles=["기존 제목 그대로입니다"],
        )
        self.assertEqual(result["topics"], {"exploit": ["밤에 자주 깨는 이유가 있습니다"]})
        self.assertEqual(
            {item["reason"] for item in result["skipped_topics"]},
            {"numeric_claim", "length", "existing_title_copy"},
        )

    def test_collected_search_phrases_reach_the_interpreter(self):
        # Autocomplete phrases are the only viewer-language source available:
        # comment bodies are deliberately not synced. They were already being
        # collected into trend_observations but never shown to the interpreter.
        captured = {}

        def interpreter(prompt):
            captured["prompt"] = prompt
            return {
                "resolved_family": "사회적고립", "family_source": "existing",
                "topics": {"exploit": ["혼자 있는 시간이 길어질 때 뇌에 생기는 변화"]},
                "evidence_relevance": [],
            }

        with tempfile.TemporaryDirectory() as tmp:
            objective_planner.plan_objective_topic(
                "reach", seed_topic="고독감", job_id="search_phrases",
                db_path=Path(tmp) / "feedback.db", allow_ai=False,
                interpreter_call=interpreter,
                trend_candidates=[
                    {"keyword": "고독감 외로움 차이", "sources": ["google_suggest"]},
                    "고독감 뜻",
                ],
            )
        self.assertIn("실제 검색어", captured["prompt"])
        self.assertIn("고독감 외로움 차이", captured["prompt"])
        self.assertIn("고독감 뜻", captured["prompt"])

    def test_interpreter_failure_falls_back_to_keyword_rules(self):
        def broken_interpreter(_prompt):
            raise objective_planner.PlannerValidationError("잘린 JSON")

        with tempfile.TemporaryDirectory() as tmp:
            plan = objective_planner.plan_objective_topic(
                "reach", seed_topic="수면", job_id="interpreter_failure",
                db_path=Path(tmp) / "feedback.db", allow_ai=False,
                interpreter_call=broken_interpreter,
            )
        self.assertEqual(plan["planning"]["seed_interpreter_status"], "failed")
        self.assertEqual(plan["content_design"]["topic_family"], "수면")
        self.assertTrue(plan["topic"])

    def test_interpreter_result_is_persisted_for_audit(self):
        def interpreter(_prompt):
            return {
                "resolved_family": "사회적고립",
                "family_source": "research_category",
                "family_reason": "외로움 관련 계열",
                "topics": {"exploit": ["혼자 있는 시간이 길어질 때 뇌에 생기는 변화"]},
                "evidence_relevance": [],
            }

        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "feedback.db"
            plan = objective_planner.plan_objective_topic(
                "reach", seed_topic="고독감", job_id="interpreter_success",
                db_path=db_path, allow_ai=False, interpreter_call=interpreter,
            )
            conn = objective_planner.feedback.connect(db_path)
            try:
                stored = conn.execute(
                    "SELECT seed_interpretation_json FROM planning_runs ORDER BY plan_id DESC LIMIT 1"
                ).fetchone()["seed_interpretation_json"]
            finally:
                conn.close()
        self.assertEqual(plan["planning"]["seed_interpreter_status"], "success")
        self.assertEqual(plan["planning"]["seed_interpreter_family"], "사회적고립")
        self.assertEqual(plan["planning"]["seed_interpreter_family_source"], "research_category")
        self.assertEqual(plan["content_design"]["topic_family"], "사회적고립")
        self.assertIn("사회적고립", stored)

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
