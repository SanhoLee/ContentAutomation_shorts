import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("korean_grammar", ROOT / "dev" / "src" / "youtube" / "korean_grammar.py")
kg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kg)


class DependentNounTests(unittest.TestCase):
    """HARD rule: these never start a caption line."""

    def test_recognises_the_nouns_this_channel_actually_uses(self):
        for word in ("수", "수도", "적", "것", "것만으로도", "게", "것과는", "중에", "가지만"):
            self.assertTrue(kg.starts_with_dependent_noun(word), word)

    def test_recognises_copula_inflections(self):
        # "아직 성장 중이기 때문이에요" — 중 + 이다 + 기
        for word in ("중이기", "것이었어요", "적이라서"):
            self.assertTrue(kg.starts_with_dependent_noun(word), word)

    def test_recognises_always_dependent_heads(self):
        for word in ("때문이에요.", "텐데요", "덕분에", "듯이"):
            self.assertTrue(kg.starts_with_dependent_noun(word), word)

    def test_does_not_fire_on_free_nouns_and_verbs(self):
        # Exact particle matching is what keeps these out; a character-class
        # test would let every one of them through.
        for word in ("수술", "수요", "중요", "필요", "게임", "가지고", "편의점"):
            self.assertFalse(kg.starts_with_dependent_noun(word), word)

    def test_copula_rule_skips_heads_that_collide_with_verbs(self):
        # "부담을 줄이는" is a verb, not 줄 + 이는.
        for word in ("줄이고", "줄이는", "줄이면"):
            self.assertFalse(kg.starts_with_dependent_noun(word), word)


class SentenceFinalTests(unittest.TestCase):
    def test_accepts_formal_and_haeyo_endings(self):
        for word in ("좋습니다", "좋아요", "그래요", "무거워요", "하세요", "거예요", "달라지거든요"):
            self.assertTrue(kg.is_sentence_final(word), word)

    def test_bare_yo_nouns_are_not_sentence_ends(self):
        # A lone "요" test would call all of these sentence-final.
        for word in ("중요", "필요", "주요", "수요", "동요"):
            self.assertFalse(kg.is_sentence_final(word), word)


class AuxiliaryVerbTests(unittest.TestCase):
    def test_detects_fused_forms(self):
        # 버리 + ㅂ니다 → 버립니다 defeats a plain startswith("버리").
        for word in ("버립니다", "드리겠습니다", "않겠냐고요", "않으셔도"):
            self.assertTrue(kg.is_auxiliary_verb(word), word)

    def test_is_soft_and_over_matches_by_design(self):
        # Documented over-match: callers must gate on ends_with_ending() first.
        self.assertTrue(kg.is_auxiliary_verb("있는"))
        self.assertFalse(kg.is_auxiliary_verb("하루"))


class ConnectiveAdverbTests(unittest.TestCase):
    def test_connective_adverbs_must_not_end_a_line(self):
        for word in ("그런데", "그리고", "하지만", "사실", "특히"):
            self.assertTrue(kg.is_no_split_after(word), word)

    def test_connective_adverb_is_not_mistaken_for_a_clause_ending(self):
        # 그런데 ends in 런데, not 는데 — it heads the next clause.
        self.assertFalse(kg.is_clause_connective("그런데"))
        self.assertTrue(kg.is_clause_connective("좋은데"))


class PriorityTests(unittest.TestCase):
    def test_ranks_sentence_final_above_connective_above_nominalizing(self):
        self.assertEqual(kg.split_priority("좋습니다"), 3)
        self.assertEqual(kg.split_priority("좋지만"), 2)
        self.assertEqual(kg.split_priority("움직이는지"), 1)
        self.assertEqual(kg.split_priority("뇌가"), 0)


class NumberTests(unittest.TestCase):
    def test_decimals_do_not_end_a_sentence(self):
        self.assertEqual(
            kg.split_into_sentences("위험은 1.32배였습니다. 네 번 이상은 1.42배."),
            ["위험은 1.32배였습니다", "네 번 이상은 1.42배"],
        )

    def test_verify_numbers_preserved(self):
        self.assertTrue(kg.verify_numbers_preserved("1.32배였고", ["1.32", "배였고"]))
        self.assertFalse(kg.verify_numbers_preserved("1.32배였고", ["1 32배였고"]))


if __name__ == "__main__":
    unittest.main()
