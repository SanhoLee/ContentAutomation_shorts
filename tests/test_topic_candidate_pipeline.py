import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

import topic_candidate_pipeline as pipeline
import topic_score
import topic_seed_pool as tsp

VOCAB = ("치매", "기억", "뇌", "수면", "운동", "예방")

RULES = {
    "threshold": 60,
    "eligible_top_k": 3,
    "weights": {
        "niche_relevance": 30, "search_intent_fit": 20, "evidence_potential": 20,
        "novelty_vs_history": 20, "safety_tone": 10,
    },
    "intent_patterns": {
        "question": ["할까", "증상", "원인", "차이", "방법"],
        "compare": ["차이", "vs", "비교"], "symptom": ["증상", "징후", "신호"],
    },
    "evidence_hints": ["연구", "예방", "인지", "수면", "운동", "치매", "기억"],
    "ban_keywords": ["완치", "보장", "기적", "100%"],
    "novelty": {"lookback_days": 60, "similarity_threshold": 0.75},
}


class NormalizeCandidateTests(unittest.TestCase):
    def test_length_bounds_filter_out_too_short_and_too_long(self):
        seed = tsp.SeedItem(seed="치매", origin="research_categories")
        self.assertIsNone(pipeline._normalize_candidate("짧", seed, VOCAB))
        self.assertIsNone(pipeline._normalize_candidate("아" * 41, seed, VOCAB))

    def test_echoing_the_seed_back_is_dropped(self):
        seed = tsp.SeedItem(seed="치매 예방", origin="research_categories")
        self.assertIsNone(pipeline._normalize_candidate("치매 예방", seed, VOCAB))

    def test_valid_suggestion_becomes_a_candidate_record(self):
        seed = tsp.SeedItem(seed="치매", origin="research_categories", category_id="memory_cognition")
        candidate = pipeline._normalize_candidate("치매 초기증상 건망증 차이", seed, VOCAB)
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate["seed"], "치매")
        self.assertEqual(candidate["category_id"], "memory_cognition")
        self.assertIn("치매", candidate["matched_terms"])


class TagDuplicatesTests(unittest.TestCase):
    def test_exact_duplicate_is_tagged(self):
        candidates = [{"title_hint": "치매 초기증상"}, {"title_hint": "치매 초기증상"}]
        pipeline._tag_duplicates(candidates)
        self.assertFalse(candidates[0]["duplicate"])
        self.assertTrue(candidates[1]["duplicate"])

    def test_near_duplicate_is_tagged(self):
        candidates = [{"title_hint": "치매 초기증상과 건망증 차이"}, {"title_hint": "치매 초기증상과 건망증 차이점"}]
        pipeline._tag_duplicates(candidates)
        self.assertTrue(candidates[1]["duplicate"])

    def test_distinct_candidates_are_not_tagged(self):
        candidates = [{"title_hint": "치매 초기증상"}, {"title_hint": "수면과 기억력 관계"}]
        pipeline._tag_duplicates(candidates)
        self.assertFalse(candidates[0]["duplicate"])
        self.assertFalse(candidates[1]["duplicate"])


class BatchExpandTests(unittest.TestCase):
    def test_off_topic_seed_yields_no_candidates_but_is_reported(self):
        seeds = [tsp.SeedItem(seed="꿈", origin="extra")]
        noise = ["꿈빛파티시엘", "꿈돌이", "꿈트렁", "꿈해몽", "기차의 꿈"]

        def fetch(url, params):
            return ["seed", noise]

        candidates, reports = pipeline.batch_expand(seeds, VOCAB, sleep_ms=0, fetch=fetch, include_youtube=False)
        self.assertEqual(candidates, [])
        self.assertEqual(len(reports), 1)
        self.assertFalse(reports[0]["ok"])

    def test_on_topic_seed_yields_candidates(self):
        seeds = [tsp.SeedItem(seed="치매", origin="research_categories")]
        good = ["치매 초기증상 건망증 차이", "치매 예방 두뇌 운동", "치매 예방 식단 연구"]

        def fetch(url, params):
            return ["seed", good]

        candidates, reports = pipeline.batch_expand(seeds, VOCAB, sleep_ms=0, fetch=fetch, include_youtube=False)
        self.assertEqual(len(candidates), 3)
        self.assertTrue(reports[0]["ok"])
        self.assertTrue(all("duplicate" in c for c in candidates))


class ScoreAndRankTests(unittest.TestCase):
    def test_below_threshold_and_banned_are_rejected_not_eligible(self):
        candidates = [
            {"title_hint": "치매 초기증상 건망증 차이는 무엇일까", "seed": "치매", "duplicate": False},
            {"title_hint": "완전히 무관한 요리 레시피 이야기", "seed": "치매", "duplicate": False},
            {"title_hint": "치매 완치 보장 100% 즉시 효과", "seed": "치매", "duplicate": False},
        ]
        eligible, rejected = pipeline.score_and_rank(candidates, vocabulary=VOCAB, recent_titles=[], rules=RULES)
        self.assertEqual(len(eligible), 1)
        reasons = {r["reject_reason"] for r in rejected}
        self.assertIn("below_threshold", reasons)
        self.assertIn("ban_keyword", reasons)

    def test_duplicate_flag_wins_over_score(self):
        candidates = [
            {"title_hint": "치매 초기증상 건망증 차이는 무엇일까", "seed": "치매", "duplicate": True},
        ]
        eligible, rejected = pipeline.score_and_rank(candidates, vocabulary=VOCAB, recent_titles=[], rules=RULES)
        self.assertEqual(eligible, [])
        self.assertEqual(rejected[0]["reject_reason"], "duplicate")

    def test_eligible_is_sorted_descending_by_score(self):
        candidates = [
            {"title_hint": "치매 초기증상 건망증 차이는 무엇일까", "seed": "치매", "duplicate": False},
            {"title_hint": "치매 예방 연구 결과와 수면 운동", "seed": "치매", "duplicate": False},
        ]
        eligible, _ = pipeline.score_and_rank(candidates, vocabulary=VOCAB, recent_titles=[], rules=RULES)
        scores = [c["score"] for c in eligible]
        self.assertEqual(scores, sorted(scores, reverse=True))


class WriteQueuesAndConsumeTests(unittest.TestCase):
    def test_write_queues_creates_files_and_run_meta(self):
        candidates = [
            {"title_hint": "치매 초기증상 건망증 차이는 무엇일까", "seed": "치매", "duplicate": False},
            {"title_hint": "치매 완치 보장 100% 즉시 효과", "seed": "치매", "duplicate": False},
        ]
        eligible, rejected = pipeline.score_and_rank(candidates, vocabulary=VOCAB, recent_titles=[], rules=RULES)
        with tempfile.TemporaryDirectory() as tmp:
            written = pipeline.write_queues(
                eligible, rejected, {"seed_count": 1, "candidate_count": 2},
                base_dir=tmp, eligible_top_k=RULES["eligible_top_k"],
            )
            self.assertTrue(written["raw"].exists())
            self.assertEqual(len(written["eligible"]), len(eligible))
            self.assertEqual(len(written["rejected"]), len(rejected))
            payload = json.loads(written["eligible"][0].read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "eligible")
            self.assertFalse(payload["consumed"])
            rejected_payload = json.loads(written["rejected"][0].read_text(encoding="utf-8"))
            self.assertEqual(rejected_payload["reject_reason"], "ban_keyword")

    def test_eligible_payload_carries_a_story_type_hint_but_no_decision(self):
        candidates = [
            {"title_hint": "치매 초기증상 흔한 오해와 진짜 신호", "seed": "치매", "duplicate": False},
        ]
        eligible, rejected = pipeline.score_and_rank(candidates, vocabulary=VOCAB, recent_titles=[], rules=RULES)
        with tempfile.TemporaryDirectory() as tmp:
            written = pipeline.write_queues(eligible, rejected, {}, base_dir=tmp, eligible_top_k=5)
            payload = json.loads(written["eligible"][0].read_text(encoding="utf-8"))
            # The genre is only spent when objective_planner consumes the
            # candidate, so the queue records a hint and leaves story_type null.
            self.assertEqual(payload["suggested_story_types"], ["myth_bust"])
            self.assertIsNone(payload["story_type"])

    def test_a_title_with_no_genre_wording_gets_an_empty_hint(self):
        candidates = [{"title_hint": "치매 예방 수면 운동 연구 정리 방법", "seed": "치매", "duplicate": False}]
        eligible, rejected = pipeline.score_and_rank(candidates, vocabulary=VOCAB, recent_titles=[], rules=RULES)
        with tempfile.TemporaryDirectory() as tmp:
            written = pipeline.write_queues(eligible, rejected, {}, base_dir=tmp, eligible_top_k=5)
            payload = json.loads(written["eligible"][0].read_text(encoding="utf-8"))
            self.assertEqual(payload["suggested_story_types"], [])

    def test_eligible_top_k_limits_written_files(self):
        candidates = [
            {"title_hint": f"치매 예방 연구 결과 {i} 회차", "seed": "치매", "duplicate": False}
            for i in range(5)
        ]
        eligible, rejected = pipeline.score_and_rank(candidates, vocabulary=VOCAB, recent_titles=[], rules=RULES)
        with tempfile.TemporaryDirectory() as tmp:
            written = pipeline.write_queues(eligible, rejected, {}, base_dir=tmp, eligible_top_k=2)
            self.assertLessEqual(len(written["eligible"]), 2)

    def test_pick_top_eligible_marks_consumed_and_skips_next_call(self):
        candidates = [
            {"title_hint": "치매 초기증상 건망증 차이는 무엇일까", "seed": "치매", "duplicate": False},
            {"title_hint": "치매 예방 연구 결과와 수면 운동", "seed": "치매", "duplicate": False},
        ]
        eligible, rejected = pipeline.score_and_rank(candidates, vocabulary=VOCAB, recent_titles=[], rules=RULES)
        with tempfile.TemporaryDirectory() as tmp:
            pipeline.write_queues(eligible, rejected, {}, base_dir=tmp, eligible_top_k=5)

            first = pipeline.pick_top_eligible(base_dir=tmp)
            self.assertIsNotNone(first)
            second = pipeline.pick_top_eligible(base_dir=tmp)
            self.assertNotEqual(first["topic_id"], second["topic_id"])

            remaining = pipeline.list_eligible(base_dir=tmp)
            self.assertEqual(len(remaining), len(eligible) - 2)

    def test_pick_top_eligible_on_empty_queue_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(pipeline.pick_top_eligible(base_dir=tmp))


class RunRefreshTests(unittest.TestCase):
    def test_refresh_without_seed_argument_writes_eligible_files(self):
        categories_fixture = {
            "categories": [{"category_id": "memory_cognition", "keywords": ["치매", "기억", "뇌"]}]
        }

        def fetch(url, params):
            return ["seed", ["치매 초기증상 건망증 차이", "치매 예방 두뇌 운동", "치매 예방 식단 연구"]]

        with tempfile.TemporaryDirectory() as tmp:
            categories_path = Path(tmp) / "categories.json"
            categories_path.write_text(json.dumps(categories_fixture, ensure_ascii=False), encoding="utf-8")

            result = pipeline.run_refresh(
                categories_path=categories_path,
                pipeline_config={**pipeline.DEFAULT_PIPELINE_CONFIG, "eligible_top_k": 5},
                score_rules=RULES,
                conn=None,
                fetch=fetch,
                base_dir=Path(tmp) / "topics",
            )
            self.assertGreater(result["run_meta"]["seed_count"], 0)
            self.assertIsNotNone(result["written"])
            self.assertTrue((Path(tmp) / "topics" / "raw").exists())

    def test_dry_run_does_not_write_files(self):
        categories_fixture = {"categories": [{"category_id": "x", "keywords": ["치매"]}]}

        def fetch(url, params):
            return ["seed", ["치매 초기증상 건망증 차이"]]

        with tempfile.TemporaryDirectory() as tmp:
            categories_path = Path(tmp) / "categories.json"
            categories_path.write_text(json.dumps(categories_fixture, ensure_ascii=False), encoding="utf-8")
            base_dir = Path(tmp) / "topics"

            result = pipeline.run_refresh(
                categories_path=categories_path, score_rules=RULES, conn=None,
                fetch=fetch, base_dir=base_dir, dry_run=True,
            )
            self.assertIsNone(result["written"])
            self.assertFalse(base_dir.exists())


if __name__ == "__main__":
    unittest.main()
