import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

import content_objectives


class ContentObjectiveTests(unittest.TestCase):
    def test_all_profiles_sum_to_one(self):
        for name, profile in content_objectives.OBJECTIVE_PROFILES.items():
            self.assertAlmostEqual(sum(profile.values()), 1.0, msg=name)

    def test_korean_aliases_and_unknown_objective(self):
        self.assertEqual(
            content_objectives.normalize_objective_type("구독자 증가 강화"),
            "subscriber_growth",
        )
        self.assertEqual(content_objectives.normalize_objective_type("균형 성장"), "balanced")
        with self.assertRaises(ValueError):
            content_objectives.normalize_objective_type("무한 성장")

    def test_target_and_horizon_are_validated(self):
        config = content_objectives.build_objective_config(
            "retention", improvement_target=0.15, horizon_days=7
        )
        self.assertEqual(config.horizon_days, 7)
        with self.assertRaises(ValueError):
            content_objectives.build_objective_config("reach", horizon_days=0)
        with self.assertRaises(ValueError):
            content_objectives.build_objective_config("reach", target_value=-1)


if __name__ == "__main__":
    unittest.main()
