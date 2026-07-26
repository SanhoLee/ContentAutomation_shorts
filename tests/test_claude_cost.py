import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

import claude_cost


class ClaudeCostTests(unittest.TestCase):
    def test_haiku_sonnet_web_and_cache_costs(self):
        haiku = claude_cost.calculate_cost(
            "claude-haiku-4-5-20251001", input_tokens=1_000_000,
            output_tokens=1_000_000, cache_creation_input_tokens=1_000_000,
            cache_read_input_tokens=1_000_000, web_search_requests=2,
        )
        self.assertAlmostEqual(haiku, 1 + 5 + 1.25 + 0.10 + 0.02)
        sonnet = claude_cost.calculate_cost(
            "claude-sonnet-4-6", input_tokens=1_000_000, output_tokens=1_000_000
        )
        self.assertAlmostEqual(sonnet, 18.0)

    def test_multiturn_usage_is_summed_and_budget_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "usage.db"
            log = Path(tmp) / "usage.jsonl"
            response = {"usage": {"input_tokens": 100_000, "output_tokens": 100_000}}
            claude_cost.record_usage(
                "web:turn_1", "claude-haiku-4-5-20251001", response,
                db_path=db, jsonl_path=log, job_id="job_1",
            )
            claude_cost.record_usage(
                "web:turn_2", "claude-haiku-4-5-20251001", response,
                db_path=db, jsonl_path=log, job_id="job_1",
            )
            self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 2)
            self.assertAlmostEqual(claude_cost.usage_total(db_path=db, job_id="job_1"), 1.2)
            with self.assertRaises(RuntimeError):
                claude_cost.assert_budget(db_path=db, job_id="job_1", job_budget_usd=1.0)


if __name__ == "__main__":
    unittest.main()
