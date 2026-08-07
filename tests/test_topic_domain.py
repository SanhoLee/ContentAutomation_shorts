import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

import topic_domain as td

SPEC = {
    "domain_id": "brain_health",
    "label_ko": "뇌 건강",
    "require_anchor": True,
    "anchor_terms": ["뇌", "치매", "기억력", "인지", "brain"],
    "seed_anchors": ["치매", "뇌"],
    "max_anchor_variants": 1,
    "category_roles": {"chronic_disease": "risk_factor", "memory_cognition": "core"},
    "default_role": "core",
}


def write_spec(tmp, overrides=None):
    path = Path(tmp) / "topic_domain.json"
    path.write_text(json.dumps({**SPEC, **(overrides or {})}, ensure_ascii=False), encoding="utf-8")
    return path


def domain(overrides=None):
    with tempfile.TemporaryDirectory() as tmp:
        return td.load_domain(write_spec(tmp, overrides))


class LoadDomainTests(unittest.TestCase):
    def test_reads_the_spec_file(self):
        loaded = domain()
        self.assertEqual(loaded.domain_id, "brain_health")
        self.assertTrue(loaded.require_anchor)
        self.assertIn("치매", loaded.anchor_terms)

    def test_missing_file_degrades_to_builtin_brain_domain(self):
        """An absent config must not quietly reopen the gate this module closes."""
        loaded = td.load_domain("/no/such/topic_domain.json")
        self.assertEqual(loaded.domain_id, td.DEFAULT_DOMAIN_SPEC["domain_id"])
        self.assertTrue(loaded.require_anchor)
        self.assertTrue(td.has_anchor("치매 예방 운동", loaded))

    def test_invalid_json_degrades_to_builtin(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertEqual(td.load_domain(path).domain_id, td.DEFAULT_DOMAIN_SPEC["domain_id"])

    def test_partial_spec_inherits_the_rest_from_builtin(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "partial.json"
            path.write_text(json.dumps({"label_ko": "다른 분야"}), encoding="utf-8")
            loaded = td.load_domain(path)
            self.assertEqual(loaded.label_ko, "다른 분야")
            self.assertEqual(loaded.anchor_terms, td._clean_terms(td.DEFAULT_DOMAIN_SPEC["anchor_terms"]))

    def test_meta_keys_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "meta.json"
            path.write_text(json.dumps({**SPEC, "_meta": {"description": "x"}}, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(td.load_domain(path).domain_id, "brain_health")

    def test_max_anchor_variants_is_at_least_one(self):
        self.assertEqual(domain({"max_anchor_variants": 0}).max_anchor_variants, 1)


class AnchorMatchingTests(unittest.TestCase):
    def test_matches_are_case_insensitive_substrings(self):
        self.assertEqual(td.matched_anchors("BRAIN health tips", domain()), ["brain"])

    def test_returns_every_distinct_hit_in_order(self):
        self.assertEqual(td.matched_anchors("뇌 건강과 치매 예방", domain()), ["뇌", "치매"])

    def test_off_domain_text_has_no_anchor(self):
        loaded = domain()
        self.assertFalse(td.has_anchor("심혈관질환 전조증상", loaded))
        self.assertFalse(td.has_anchor("고혈압 증상", loaded))

    def test_the_regression_case_is_covered(self):
        """`뇌 건강 식단` used to score below `심혈관질환 증상`."""
        loaded = domain()
        self.assertTrue(td.has_anchor("뇌 건강 식단", loaded))


class RoleTests(unittest.TestCase):
    def test_known_categories_use_their_configured_role(self):
        loaded = domain()
        self.assertEqual(td.role_for("chronic_disease", loaded), td.ROLE_RISK_FACTOR)
        self.assertEqual(td.role_for("memory_cognition", loaded), td.ROLE_CORE)

    def test_unknown_and_missing_category_fall_back_to_default_role(self):
        loaded = domain()
        self.assertEqual(td.role_for("brand_new_category", loaded), td.ROLE_CORE)
        self.assertEqual(td.role_for(None, loaded), td.ROLE_CORE)

    def test_default_role_is_configurable(self):
        loaded = domain({"default_role": td.ROLE_RISK_FACTOR})
        self.assertEqual(td.role_for(None, loaded), td.ROLE_RISK_FACTOR)


class AnchorSeedTests(unittest.TestCase):
    def test_risk_factor_seed_is_anchored(self):
        self.assertEqual(td.anchor_seed("고혈압", "chronic_disease", domain()), ["치매 고혈압"])

    def test_core_seed_is_left_alone(self):
        self.assertEqual(td.anchor_seed("기억력", "memory_cognition", domain()), ["기억력"])

    def test_seed_that_already_carries_an_anchor_is_not_double_anchored(self):
        loaded = domain()
        self.assertEqual(td.anchor_seed("뇌혈관", "chronic_disease", loaded), ["뇌혈관"])

    def test_max_anchor_variants_caps_the_expansion(self):
        loaded = domain({"max_anchor_variants": 2})
        self.assertEqual(td.anchor_seed("고혈압", "chronic_disease", loaded), ["치매 고혈압", "뇌 고혈압"])

    def test_variants_carry_the_anchor_label(self):
        self.assertEqual(td.anchor_variants("고혈압", "chronic_disease", domain()), [("치매 고혈압", "치매")])
        self.assertEqual(td.anchor_variants("기억력", "memory_cognition", domain()), [("기억력", None)])

    def test_empty_seed_yields_nothing(self):
        self.assertEqual(td.anchor_seed("   ", "chronic_disease", domain()), [])

    def test_no_seed_anchors_configured_leaves_seeds_bare(self):
        self.assertEqual(td.anchor_seed("고혈압", "chronic_disease", domain({"seed_anchors": []})), ["고혈압"])

    def test_latin_seed_gets_a_latin_anchor(self):
        """`치매 diabetes` is a query nobody types; research_categories.json
        genuinely holds `diabetes`, `hypertension`, `cholesterol`."""
        loaded = domain({"seed_anchors_latin": ["dementia", "brain"]})
        self.assertEqual(td.anchor_seed("diabetes", "chronic_disease", loaded), ["dementia diabetes"])
        self.assertEqual(td.anchor_seed("고혈압", "chronic_disease", loaded), ["치매 고혈압"])

    def test_latin_seed_falls_back_to_korean_anchor_when_none_configured(self):
        loaded = domain({"seed_anchors_latin": []})
        self.assertEqual(td.anchor_seed("diabetes", "chronic_disease", loaded), ["치매 diabetes"])


class StripAnchorTests(unittest.TestCase):
    def test_removes_only_a_leading_anchor(self):
        self.assertEqual(td.strip_anchor("치매 고혈압", "치매"), "고혈압")

    def test_no_anchor_is_a_passthrough(self):
        self.assertEqual(td.strip_anchor("고혈압", None), "고혈압")
        self.assertEqual(td.strip_anchor("고혈압 치매", "치매"), "고혈압 치매")


class ShippedConfigTests(unittest.TestCase):
    """The committed dev/config/topic_domain.json is the live contract."""

    def test_shipped_config_gates_the_observed_bad_candidates(self):
        loaded = td.load_domain()
        self.assertTrue(loaded.require_anchor)
        for bad in ("심혈관질환 전조증상", "심혈관질환 증상", "고혈압 증상"):
            self.assertFalse(td.has_anchor(bad, loaded), bad)
        for good in ("뇌 건강 식단", "치매 예방 운동 방법", "고혈압 치매 위험"):
            self.assertTrue(td.has_anchor(good, loaded), good)

    def test_shipped_config_marks_chronic_disease_as_risk_factor(self):
        loaded = td.load_domain()
        self.assertEqual(td.role_for("chronic_disease", loaded), td.ROLE_RISK_FACTOR)
        self.assertEqual(td.anchor_seed("심혈관", "chronic_disease", loaded), ["치매 심혈관"])


if __name__ == "__main__":
    unittest.main()
