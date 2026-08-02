import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

import topic_score as ts

VOCAB = ("치매", "기억", "뇌", "수면", "운동", "예방")

RULES = {
    "threshold": 60,
    "eligible_top_k": 15,
    "weights": {
        "niche_relevance": 30,
        "search_intent_fit": 20,
        "evidence_potential": 20,
        "novelty_vs_history": 20,
        "safety_tone": 10,
    },
    "intent_patterns": {
        "question": ["할까", "증상", "원인", "차이", "방법"],
        "compare": ["차이", "vs", "비교"],
        "symptom": ["증상", "징후", "신호"],
    },
    "evidence_hints": ["연구", "예방", "인지", "수면", "운동", "치매", "기억"],
    "ban_keywords": ["완치", "보장", "기적", "100%"],
    "novelty": {"lookback_days": 60, "similarity_threshold": 0.75},
}


class LoadRulesTests(unittest.TestCase):
    def test_missing_file_falls_back_to_defaults(self):
        rules = ts.load_rules("/no/such/path/rules.json")
        self.assertEqual(rules["threshold"], ts.DEFAULT_RULES["threshold"])
        self.assertEqual(rules["weights"], ts.DEFAULT_RULES["weights"])

    def test_repo_config_file_loads(self):
        rules = ts.load_rules()
        self.assertEqual(sum(rules["weights"].values()), 100)


class ComponentScoreTests(unittest.TestCase):
    def test_niche_relevance_scales_with_vocabulary_hits(self):
        score, matched = ts.score_niche_relevance("치매 초기증상과 건망증 차이", VOCAB, 30)
        self.assertIn("치매", matched)
        self.assertGreater(score, 0)

    def test_niche_relevance_zero_without_any_hit(self):
        score, matched = ts.score_niche_relevance("아무 상관 없는 문장", VOCAB, 30)
        self.assertEqual(score, 0)
        self.assertEqual(matched, [])

    def test_search_intent_fit_rewards_question_patterns(self):
        score = ts.score_search_intent_fit("치매 초기증상은 무엇일까", RULES["intent_patterns"], 20)
        self.assertGreater(score, 0)

    def test_evidence_potential_rewards_hint_terms(self):
        score = ts.score_evidence_potential("수면과 치매 예방 연구 결과", RULES["evidence_hints"], 20)
        self.assertGreater(score, 0)

    def test_novelty_full_score_with_no_history(self):
        score = ts.score_novelty_vs_history("치매 초기증상", [], 20)
        self.assertEqual(score, 20.0)

    def test_novelty_drops_for_near_duplicate_title(self):
        score = ts.score_novelty_vs_history("치매 초기증상과 건망증 차이", ["치매 초기증상과 건망증 차이점"], 20)
        self.assertLess(score, 10)

    def test_safety_tone_zeroes_out_on_ban_keyword(self):
        score, banned = ts.score_safety_tone("치매 완치 방법", RULES["ban_keywords"], 10)
        self.assertTrue(banned)
        self.assertEqual(score, 0)

    def test_safety_tone_full_without_ban_keyword(self):
        score, banned = ts.score_safety_tone("치매 예방 습관", RULES["ban_keywords"], 10)
        self.assertFalse(banned)
        self.assertEqual(score, 10)


class ScoreCandidateTests(unittest.TestCase):
    def test_strong_candidate_clears_threshold(self):
        result = ts.score_candidate(
            "치매 초기증상 건망증 차이는 무엇일까",
            vocabulary=VOCAB, recent_titles=[], rules=RULES,
        )
        self.assertGreaterEqual(result.total, RULES["threshold"])
        self.assertFalse(result.banned)

    def test_ban_keyword_forces_low_score_and_flag(self):
        result = ts.score_candidate(
            "치매 완치 보장 100% 즉시 효과",
            vocabulary=VOCAB, recent_titles=[], rules=RULES,
        )
        self.assertTrue(result.banned)
        self.assertEqual(result.breakdown["safety_tone"], 0)

    def test_off_topic_candidate_scores_low(self):
        result = ts.score_candidate(
            "완전히 무관한 요리 레시피 이야기",
            vocabulary=VOCAB, recent_titles=[], rules=RULES,
        )
        self.assertLess(result.total, RULES["threshold"])

    def test_total_is_sum_of_breakdown(self):
        result = ts.score_candidate(
            "수면과 기억력 연구 결과 정리",
            vocabulary=VOCAB, recent_titles=[], rules=RULES,
        )
        self.assertEqual(result.total, int(round(sum(result.breakdown.values()))))


if __name__ == "__main__":
    unittest.main()
