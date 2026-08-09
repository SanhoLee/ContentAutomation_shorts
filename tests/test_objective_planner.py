import os
import random
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

# Planning is otherwise offline; the evidence probe is the one part that calls
# out to Europe PMC. Left on, this suite would issue a request per candidate.
# The ladder itself is covered by tests/test_evidence_probe.py against stubs.
os.environ["EVIDENCE_PROBE_ENABLED"] = "0"

import objective_planner
import story_types


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

    def test_planner_hook_type_survives_as_a_retired_label(self):
        # 질문형 is retired (merged into 호기심갭형) but Haiku still answers it out
        # of habit. Before normalize() ran here, this candidate was skipped
        # outright instead of landing on the label the DB now expects.
        candidates = [{"candidate_id": "cand_01", "topic": "수면 신호", "evidence_refs": []}]
        valid = objective_planner.validate_planner_output(
            {"candidates": [{"candidate_id": "cand_01", "hook_type": "질문형"}]},
            candidates, valid_refs=set(), existing_video_ids=set(),
        )
        self.assertEqual(valid["candidates"][0]["hook_type"], "호기심갭형")
        self.assertEqual(valid["skipped_candidates"], [])

    def test_planner_hook_type_still_rejects_a_genuinely_unknown_value(self):
        # A second, clean candidate keeps the whole call from raising (that only
        # happens when every candidate is dropped), so the skip itself is visible.
        candidates = [
            {"candidate_id": "cand_01", "topic": "수면 신호", "evidence_refs": []},
            {"candidate_id": "cand_02", "topic": "기억력 습관", "evidence_refs": []},
        ]
        valid = objective_planner.validate_planner_output(
            {"candidates": [
                {"candidate_id": "cand_01", "hook_type": "완전히엉뚱한값"},
                {"candidate_id": "cand_02"},
            ]},
            candidates, valid_refs=set(), existing_video_ids=set(),
        )
        self.assertEqual([c["candidate_id"] for c in valid["candidates"]], ["cand_02"])
        self.assertEqual(len(valid["skipped_candidates"]), 1)
        skipped = valid["skipped_candidates"][0]
        self.assertEqual(skipped["candidate_id"], "cand_01")
        self.assertIn("완전히엉뚱한값", skipped["reason"])

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

    def test_dynamic_decision_threshold_falls_back_when_no_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = objective_planner.feedback.connect(Path(tmp) / "feedback.db")
            try:
                threshold, count = objective_planner._dynamic_decision_threshold(
                    conn, percentile=0.5, default=55.0,
                )
            finally:
                conn.close()
        self.assertEqual((threshold, count), (55.0, 0))

    def test_dynamic_decision_threshold_interpolates_percentile_from_history(self):
        scores = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0]
        with tempfile.TemporaryDirectory() as tmp:
            conn = objective_planner.feedback.connect(Path(tmp) / "feedback.db")
            try:
                conn.execute(
                    """INSERT INTO objectives (
                        objective_id, objective_type, weights_json, created_at, updated_at
                    ) VALUES (1, 'reach', '{}', '2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00')"""
                )
                for i, score in enumerate(scores):
                    conn.execute(
                        """INSERT INTO planning_runs (
                            job_id, objective_id, candidate_pool_json, selection_mode,
                            base_score, adjusted_score, confidence, decision, created_at
                        ) VALUES (?, 1, '[]', 'exploit', ?, ?, 0.3, 'manual_review', ?)""",
                        (f"job_{i}", score, score, f"2026-07-{i + 1:02d}T00:00:00+00:00"),
                    )
                conn.commit()

                median, count = objective_planner._dynamic_decision_threshold(conn, percentile=0.5)
                quartile, _ = objective_planner._dynamic_decision_threshold(conn, percentile=0.25)
                interpolated, _ = objective_planner._dynamic_decision_threshold(conn, percentile=0.6)
            finally:
                conn.close()

        self.assertEqual(count, 9)
        self.assertAlmostEqual(median, 50.0)
        self.assertAlmostEqual(quartile, 30.0)
        self.assertAlmostEqual(interpolated, 58.0)

    def test_dynamic_confidence_threshold_falls_back_when_no_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = objective_planner.feedback.connect(Path(tmp) / "feedback.db")
            try:
                threshold, count = objective_planner._dynamic_confidence_threshold(
                    conn, percentile=0.5, default=0.6,
                )
            finally:
                conn.close()
        self.assertEqual((threshold, count), (0.6, 0))

    def test_dynamic_confidence_threshold_interpolates_percentile_from_history(self):
        confidences = [0.017, 0.185, 0.214, 0.214, 0.221, 0.227, 0.227, 0.265, 0.295, 0.324, 0.667]
        with tempfile.TemporaryDirectory() as tmp:
            conn = objective_planner.feedback.connect(Path(tmp) / "feedback.db")
            try:
                conn.execute(
                    """INSERT INTO objectives (
                        objective_id, objective_type, weights_json, created_at, updated_at
                    ) VALUES (1, 'reach', '{}', '2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00')"""
                )
                for i, confidence in enumerate(confidences):
                    conn.execute(
                        """INSERT INTO planning_runs (
                            job_id, objective_id, candidate_pool_json, selection_mode,
                            base_score, adjusted_score, confidence, decision, created_at
                        ) VALUES (?, 1, '[]', 'exploit', 50.0, 50.0, ?, 'manual_review', ?)""",
                        (f"job_{i}", confidence, f"2026-07-{i + 1:02d}T00:00:00+00:00"),
                    )
                conn.commit()

                median, count = objective_planner._dynamic_confidence_threshold(conn, percentile=0.5)
            finally:
                conn.close()

        self.assertEqual(count, 11)
        self.assertAlmostEqual(median, 0.227)

    def test_confidence_threshold_shifts_manual_review_boundary(self):
        # base_score 40.0 minus the neutral critic-risk penalty (5.0) settles
        # adjusted_score at 35.0, below decision_threshold=40.0 either way, so
        # only the confidence gate decides manual_review vs rejected here.
        strict_gate = objective_planner.judge_candidate(
            {"confidence": 0.3, "duplicate_similarity": 0.0},
            {"base_score": 40.0},
            None,
            desired_exploration="exploit",
            decision_threshold=40.0,
            confidence_threshold=0.6,
        )
        self.assertEqual(strict_gate["decision"], "manual_review")

        lowered_gate = objective_planner.judge_candidate(
            {"confidence": 0.3, "duplicate_similarity": 0.0},
            {"base_score": 40.0},
            None,
            desired_exploration="exploit",
            decision_threshold=40.0,
            confidence_threshold=0.25,
        )
        self.assertEqual(lowered_gate["decision"], "rejected")

    def test_job_rng_is_reproducible_per_job_and_differs_across_jobs(self):
        self.assertEqual(
            objective_planner.job_rng("job_a").random(),
            objective_planner.job_rng("job_a").random(),
        )
        self.assertNotEqual(
            objective_planner.job_rng("job_a").random(),
            objective_planner.job_rng("job_b").random(),
        )

    def _pool_topics(self, db_path, job_id):
        conn = objective_planner.feedback.connect(db_path)
        try:
            return [
                item["topic"] for item in objective_planner.build_candidate_pool(
                    conn, objective_type="reach", rng=objective_planner.job_rng(job_id),
                )
            ]
        finally:
            conn.close()

    def test_auto_discovery_pool_varies_by_job_but_repeats_for_the_same_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "feedback.db"
            first = self._pool_topics(db_path, "goal_20260729_000001")
            again = self._pool_topics(db_path, "goal_20260729_000001")
            other = self._pool_topics(db_path, "goal_20260729_999999")

        self.assertTrue(first)
        self.assertEqual(first, again)
        self.assertNotEqual(first, other)

    def _banded(self, topic, score, candidate_id="cand_01"):
        return {
            "candidate": {"candidate_id": candidate_id, "topic": topic},
            "judgment": {"adjusted_score": score},
        }

    def test_band_selection_can_pick_a_runner_up_within_the_band(self):
        eligible = [self._banded("주제 A", 50.0, "cand_01"), self._banded("주제 B", 47.0, "cand_02")]
        picks = {
            objective_planner.select_within_band(
                eligible, random.Random(seed), band=6.0,
                existing_titles=[], duplicate_threshold=0.25,
            )[0]["candidate"]["candidate_id"]
            for seed in range(20)
        }
        self.assertEqual(picks, {"cand_01", "cand_02"})

    def test_zero_band_keeps_strict_top_one_selection(self):
        eligible = [self._banded("주제 A", 50.0, "cand_01"), self._banded("주제 B", 47.0, "cand_02")]
        for seed in range(10):
            selected, audit = objective_planner.select_within_band(
                eligible, random.Random(seed), band=0.0,
                existing_titles=[], duplicate_threshold=0.25,
            )
            self.assertEqual(selected["candidate"]["candidate_id"], "cand_01")
            self.assertEqual(audit["selection_band_size"], 1)

    def test_band_selection_never_picks_a_near_duplicate_of_an_existing_title(self):
        existing = "냉동 블루베리 안토시아닌, 신선한 것보다 좋다"
        eligible = [
            self._banded("전혀 다른 새 주제 후보", 50.0, "cand_01"),
            self._banded(existing, 49.5, "cand_02"),
        ]
        for seed in range(20):
            selected, audit = objective_planner.select_within_band(
                eligible, random.Random(seed), band=6.0,
                existing_titles=[existing], duplicate_threshold=0.25,
            )
            self.assertEqual(selected["candidate"]["candidate_id"], "cand_01")
            self.assertEqual(audit["selection_duplicate_filtered"], 1)

    def test_band_selection_falls_back_to_top_when_every_candidate_is_duplicate(self):
        existing = "냉동 블루베리 안토시아닌, 신선한 것보다 좋다"
        eligible = [self._banded(existing, 50.0, "cand_01")]
        selected, audit = objective_planner.select_within_band(
            eligible, random.Random(0), band=6.0,
            existing_titles=[existing], duplicate_threshold=0.25,
        )
        self.assertEqual(selected["candidate"]["candidate_id"], "cand_01")
        self.assertEqual(audit["selection_pool_size"], 1)

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

    def test_format_hook_repeat_penalty_survives_legacy_db_labels(self):
        # content_features still holds retired labels (질문형, not 호기심갭형).
        # Before normalize() ran on both sides of this comparison, every DB row
        # used the old vocabulary and the comparison could never match, so the
        # monotony penalty silently never fired again.
        original_default_item = objective_planner._default_planner_item

        def run(tmp, *, db_hook_type):
            # allow_ai=False routes every candidate through _default_planner_item
            # (no live planner/critic call), so forcing its output here is enough
            # to make every candidate in the pool carry the same format/hook --
            # otherwise whichever candidate wins selection might be a sibling
            # that never touches the DB row this test seeded, masking the result.
            def forced_default(candidate):
                item = original_default_item(candidate)
                return {**item, "format_type": "오해반전형", "hook_type": "질문형"}

            db_path = Path(tmp) / "feedback.db"
            conn = objective_planner.feedback.connect(db_path)
            now = objective_planner.utc_now()
            conn.execute(
                "INSERT INTO videos (video_id, title, published_at, fetched_at) VALUES (?, ?, ?, ?)",
                ("v1", "완전히 다른 영상 제목입니다", now, now),
            )
            conn.execute(
                "INSERT INTO content_features (video_id, format_type, hook_type, plan_json, created_at) "
                "VALUES (?, ?, ?, '{}', ?)",
                ("v1", "오해반전형", db_hook_type, now),
            )
            conn.commit()
            conn.close()

            objective_planner._default_planner_item = forced_default
            try:
                plan = objective_planner.plan_objective_topic(
                    "reach", seed_topic="수면", job_id="hook_repeat_job",
                    db_path=db_path, allow_ai=False,
                )
            finally:
                objective_planner._default_planner_item = original_default_item
            return plan["objective"]["base_score"] - plan["objective"]["adjusted_score"]

        with tempfile.TemporaryDirectory() as tmp_match, tempfile.TemporaryDirectory() as tmp_diff:
            # DB has legacy "질문형"; forced planner item also answers "질문형" ->
            # both normalize to 호기심갭형 -> same pair -> penalty fires.
            matched_gap = run(tmp_match, db_hook_type="질문형")
            # DB history uses a different pattern -> no repeat -> no penalty.
            mismatched_gap = run(tmp_diff, db_hook_type="반전형")
        self.assertEqual(matched_gap - mismatched_gap, 10.0)


class StoryTypeSelectionTests(unittest.TestCase):
    """The genre the planner hands to Stage 1 (design spec §4.4)."""

    def setUp(self):
        # resolve_story_type reads the live work root for history; point it at
        # an empty one so these assertions do not depend on the host's jobs.
        self._work_root = tempfile.TemporaryDirectory()
        self._previous = os.environ.get("WORK_DIR_BASE")
        os.environ["WORK_DIR_BASE"] = self._work_root.name

    def tearDown(self):
        if self._previous is None:
            os.environ.pop("WORK_DIR_BASE", None)
        else:
            os.environ["WORK_DIR_BASE"] = self._previous
        self._work_root.cleanup()

    def selected(self, format_type="오해반전형", **candidate):
        return {
            "candidate": {"candidate_id": "c1", "topic": "수면과 기억", **candidate},
            "planner": {"topic": "수면과 기억", "format_type": format_type, "hook_type": "반전형"},
            "judgment": {
                "confidence": 0.5, "decision": "limited_test",
                "base_score": 50.0, "adjusted_score": 50.0,
            },
            "objective_type": "subscriber_growth",
        }

    def test_topic_plan_always_records_a_story_type(self):
        plan = objective_planner._topic_plan(self.selected(), plan_id=1, objective_id=1)
        self.assertIn(plan["story_type"], story_types.STORY_TYPES)
        self.assertEqual(plan["content_design"]["story_type"], plan["story_type"])

    def test_story_type_rewrites_a_format_it_disagrees_with(self):
        plan = objective_planner._topic_plan(
            self.selected(format_type="사례추적형"), plan_id=1, objective_id=1,
            story_type="habit_mechanism",
        )
        self.assertEqual(plan["story_type"], "habit_mechanism")
        self.assertEqual(plan["content_design"]["format_type"], "행동챌린지형")

    def test_a_consistent_format_is_left_alone(self):
        plan = objective_planner._topic_plan(
            self.selected(format_type="비교형"), plan_id=1, objective_id=1, story_type="myth_bust",
        )
        self.assertEqual(plan["content_design"]["format_type"], "비교형")

    def test_empty_history_picks_the_highest_weighted_genre(self):
        self.assertEqual(objective_planner.resolve_story_type(None, self.selected()), "principle_experience")

    def test_a_candidate_hint_narrows_the_choice(self):
        picked = objective_planner.resolve_story_type(
            None, self.selected(suggested_story_types=["habit_mechanism", "case_journey"]),
        )
        self.assertEqual(picked, "habit_mechanism")

    def test_disabling_enforcement_defers_to_the_planner_format(self):
        with unittest.mock.patch.object(
            story_types, "load_config",
            return_value={**story_types.DEFAULT_CONFIG, "enforce_on_auto": False},
        ):
            picked = objective_planner.resolve_story_type(None, self.selected(format_type="사례추적형"))
        self.assertEqual(picked, "case_journey")

    def test_end_to_end_plan_carries_the_genre_to_stage_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = objective_planner.plan_objective_topic(
                "subscriber_growth", seed_topic="수면", job_id="fixed_job",
                output_path=Path(tmp) / "topic_plan.json",
                db_path=Path(tmp) / "feedback.db", allow_ai=False,
            )
            self.assertIn(plan["story_type"], story_types.STORY_TYPES)
            self.assertEqual(plan["content_design"]["story_type"], plan["story_type"])

    def test_recent_history_moves_the_next_pick_off_the_over_produced_genre(self):
        for index in range(6):
            job_dir = Path(self._work_root.name) / f"job_{index}"
            job_dir.mkdir()
            (job_dir / "strategy.json").write_text(
                '{"story_type": "principle_experience"}', encoding="utf-8",
            )
        self.assertNotEqual(
            objective_planner.resolve_story_type(None, self.selected()), "principle_experience",
        )


if __name__ == "__main__":
    unittest.main()
