import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

import topic_domain
import topic_score as ts

VOCAB = ("치매", "기억", "뇌", "수면", "운동", "예방")

DOMAIN = topic_domain.Domain(
    domain_id="brain_health", label_ko="뇌 건강", require_anchor=True,
    anchor_terms=("뇌", "치매", "기억", "기억력", "인지", "건망증"),
    seed_anchors=("치매", "뇌"), max_anchor_variants=1,
    category_roles={"chronic_disease": "risk_factor"}, default_role="core",
)
PERMISSIVE_DOMAIN = dataclasses.replace(DOMAIN, require_anchor=False)

RULES = {
    "threshold": 45,
    "eligible_top_k": 15,
    "weights": {
        "domain_relevance": 30,
        "niche_relevance": 15,
        "search_intent_fit": 15,
        "evidence_potential": 20,
        "novelty_vs_history": 15,
        "safety_tone": 5,
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


class DomainRelevanceTests(unittest.TestCase):
    def test_one_anchor_scores_partial_two_scores_full(self):
        one, hits_one = ts.score_domain_relevance("치매 예방 습관", DOMAIN, 30)
        two, hits_two = ts.score_domain_relevance("치매와 기억력 이야기", DOMAIN, 30)
        self.assertEqual(len(hits_one), 1)
        self.assertEqual(round(one, 2), 20.0)
        self.assertGreaterEqual(len(hits_two), 2)
        self.assertEqual(two, 30.0)

    def test_no_anchor_scores_zero(self):
        score, hits = ts.score_domain_relevance("심혈관질환 전조증상", DOMAIN, 30)
        self.assertEqual(score, 0.0)
        self.assertEqual(hits, [])

    def test_score_never_exceeds_its_weight(self):
        score, _ = ts.score_domain_relevance("뇌 치매 기억 인지 건망증", DOMAIN, 30)
        self.assertEqual(score, 30.0)


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
    def _score(self, text, domain=DOMAIN):
        return ts.score_candidate(text, vocabulary=VOCAB, recent_titles=[], rules=RULES, domain=domain)

    def test_strong_candidate_clears_threshold(self):
        result = self._score("치매 초기증상 건망증 차이는 무엇일까")
        self.assertGreaterEqual(result.total, RULES["threshold"])
        self.assertFalse(result.banned)
        self.assertFalse(result.missing_anchor)

    def test_ban_keyword_forces_low_score_and_flag(self):
        result = self._score("치매 완치 보장 100% 즉시 효과")
        self.assertTrue(result.banned)
        self.assertEqual(result.breakdown["safety_tone"], 0)

    def test_off_topic_candidate_scores_low(self):
        result = self._score("완전히 무관한 요리 레시피 이야기")
        self.assertLess(result.total, RULES["threshold"])

    def test_total_is_sum_of_breakdown(self):
        result = self._score("수면과 기억력 연구 결과 정리")
        self.assertEqual(result.total, int(round(sum(result.breakdown.values()))))

    def test_missing_anchor_is_flagged(self):
        result = self._score("심혈관질환 전조증상")
        self.assertTrue(result.missing_anchor)
        self.assertEqual(result.matched_anchors, ())
        self.assertEqual(result.breakdown["domain_relevance"], 0)

    def test_matched_anchors_are_reported(self):
        result = self._score("치매 예방 기억력 훈련")
        self.assertIn("치매", result.matched_anchors)
        self.assertIn("기억력", result.matched_anchors)

    def test_gate_is_off_when_the_domain_does_not_require_an_anchor(self):
        result = self._score("심혈관질환 전조증상", domain=PERMISSIVE_DOMAIN)
        self.assertFalse(result.missing_anchor)
        self.assertEqual(result.breakdown["domain_relevance"], 0)

class ShippedConfigCalibrationTests(unittest.TestCase):
    """Scored against the committed rules/domain and the category vocabulary, so
    a weight or threshold edit that breaks the real separation fails here.

    Vocabulary comes from research_categories.json alone (conn=None): the
    keywords table lives on the server and only ever *adds* niche points, so a
    candidate that clears the threshold here clears it in production too.
    """

    @classmethod
    def setUpClass(cls):
        import trend_probe
        cls.rules = ts.load_rules()
        cls.domain = topic_domain.load_domain()
        cls.vocab = trend_probe.build_channel_vocabulary(None)
        # Production never has an empty title history, so novelty is never full
        # weight. Two representative titles keep this honest without a DB.
        cls.recent = ["치매 예방에 좋은 습관 5가지", "기억력 지키는 아침 루틴"]

    def _score(self, text):
        return ts.score_candidate(
            text, vocabulary=self.vocab, recent_titles=self.recent,
            rules=self.rules, domain=self.domain,
        )

    def test_observed_bad_candidates_are_gated_out(self):
        """The three topics from the Slack card that started this."""
        for text in ("심혈관질환 전조증상", "심혈관질환 증상", "고혈압 증상"):
            with self.subTest(text=text):
                self.assertTrue(self._score(text).missing_anchor)

    def test_in_domain_candidates_clear_the_calibrated_threshold(self):
        threshold = self.rules["threshold"]
        for text in ("치매 예방 운동 방법", "뇌 건강 식단", "고혈압 치매 위험",
                     "수면 부족 기억력 저하", "난청 치매 위험", "기억력 좋아지는 음식"):
            with self.subTest(text=text):
                result = self._score(text)
                self.assertFalse(result.missing_anchor)
                self.assertGreaterEqual(result.total, threshold)

    def test_anchor_alone_does_not_clear_the_threshold(self):
        """The gate handles topicality; the threshold still has to demand
        substance, or every anchored string would qualify."""
        self.assertLess(self._score("뇌 이야기 하나").total, self.rules["threshold"])

    def test_the_observed_regression_is_inverted(self):
        """`심혈관질환 증상` used to beat `뇌 건강 식단` 60 to 57."""
        cardio = self._score("심혈관질환 증상")
        brain = self._score("뇌 건강 식단")
        self.assertTrue(cardio.missing_anchor)
        self.assertFalse(brain.missing_anchor)
        self.assertGreater(brain.total, cardio.total)


if __name__ == "__main__":
    unittest.main()
