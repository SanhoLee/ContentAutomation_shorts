import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

import hook_types as ht

# The labels actually sitting in content_features.hook_type when the
# vocabularies were merged. This list is the migration contract: if any of these
# stops resolving, published videos silently drop out of every hook aggregate
# and out of the format_hook_repeat comparison.
LABELS_IN_PRODUCTION = ["경고형", "도전형", "반전형", "발견형", "질문형"]


class NormalizeTests(unittest.TestCase):
    def test_canonical_values_are_idempotent(self):
        for pattern in ht.HOOK_PATTERNS:
            self.assertEqual(ht.normalize(pattern), pattern)

    def test_every_legacy_label_resolves_to_a_canonical_pattern(self):
        for legacy, expected in ht.LEGACY_HOOKS.items():
            with self.subTest(legacy=legacy):
                self.assertIn(expected, ht.HOOK_PATTERNS)
                self.assertEqual(ht.normalize(legacy), expected)

    def test_labels_already_in_the_database_all_resolve(self):
        for legacy in LABELS_IN_PRODUCTION:
            with self.subTest(legacy=legacy):
                self.assertIn(ht.normalize(legacy), ht.HOOK_PATTERNS)

    def test_retired_labels_land_where_the_merge_intended(self):
        self.assertEqual(ht.normalize("질문형"), ht.CURIOSITY_GAP)
        self.assertEqual(ht.normalize("숫자충격형"), ht.NUMBER)
        self.assertEqual(ht.normalize("발견형"), ht.EXPOSE)
        # 공감형 and 도전형 both collapse onto 지목형.
        self.assertEqual(ht.normalize("공감형"), ht.CALLOUT)
        self.assertEqual(ht.normalize("도전형"), ht.CALLOUT)
        self.assertEqual(ht.normalize("두려움형"), ht.WARNING)

    def test_unknown_input_is_none_rather_than_a_guess(self):
        for value in ("", None, "  ", "완전히엉뚱한값", 0, []):
            with self.subTest(value=value):
                self.assertIsNone(ht.normalize(value))

    def test_surrounding_whitespace_is_tolerated(self):
        self.assertEqual(ht.normalize("  질문형  "), ht.CURIOSITY_GAP)


class RuleTests(unittest.TestCase):
    def test_every_pattern_has_a_rule(self):
        self.assertEqual(set(ht.HOOK_RULES), set(ht.HOOK_PATTERNS))
        for pattern, rule in ht.HOOK_RULES.items():
            with self.subTest(pattern=pattern):
                self.assertTrue(rule.strip())

    def test_number_rule_still_bans_vague_quantifiers(self):
        # The whole point of 숫자형 is that a real figure beats a round one.
        rule = ht.HOOK_RULES[ht.NUMBER]
        self.assertIn("대부분", rule)
        self.assertIn("금지", rule)

    def test_curiosity_gap_rule_still_demands_a_concrete_noun(self):
        rule = ht.HOOK_RULES[ht.CURIOSITY_GAP]
        self.assertIn("이 방법", rule)
        self.assertIn("구체 명사", rule)
        self.assertIn("✓", rule)

    def test_rules_avoid_story_type_marker_strings(self):
        # The hook block renders in both USE_STORY_TYPES modes, and
        # test_story_type_pipeline asserts these strings are absent when off.
        blob = ht.prompt_block() + "".join(ht.HOOK_RULES.values())
        for marker in ("스토리 타입", "must_show", '"role"'):
            with self.subTest(marker=marker):
                self.assertNotIn(marker, blob)


class PromptBlockTests(unittest.TestCase):
    def test_renders_every_pattern(self):
        block = ht.prompt_block()
        for pattern in ht.HOOK_PATTERNS:
            with self.subTest(pattern=pattern):
                self.assertIn(pattern, block)
        self.assertEqual(len(block.splitlines()), len(ht.HOOK_PATTERNS))

    def test_assigned_pattern_is_listed_first(self):
        block = ht.prompt_block(ht.EXPOSE)
        self.assertTrue(block.splitlines()[0].startswith(f"  · {ht.EXPOSE}:"))
        self.assertEqual(len(block.splitlines()), len(ht.HOOK_PATTERNS))

    def test_legacy_assignment_is_normalized_before_ordering(self):
        block = ht.prompt_block("질문형")
        self.assertTrue(block.splitlines()[0].startswith(f"  · {ht.CURIOSITY_GAP}:"))

    def test_unknown_assignment_falls_back_to_canonical_order(self):
        self.assertEqual(ht.prompt_block("없는패턴"), ht.prompt_block())


class DescribeTests(unittest.TestCase):
    def test_describe_joins_label_and_rule(self):
        self.assertEqual(ht.describe(ht.NUMBER), f"{ht.NUMBER} — {ht.HOOK_RULES[ht.NUMBER]}")

    def test_describe_accepts_legacy_and_rejects_unknown(self):
        self.assertTrue(ht.describe("질문형").startswith(ht.CURIOSITY_GAP))
        self.assertEqual(ht.describe("없는패턴"), "")


if __name__ == "__main__":
    unittest.main()
