import argparse
import importlib.util
import sys
import tempfile
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dev" / "src"
sys.path.insert(0, str(SRC / "common"))
sys.path.insert(0, str(SRC / "youtube"))

import topic_candidate_pipeline as pipeline


def load_module(name, filename):
    spec = importlib.util.spec_from_file_location(name, SRC / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


topic_plan = load_module("phase1_topic_plan", "common/0_topic_plan.py")


class FromEligibleWiringTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.original_dir = pipeline.DATA_TOPICS_DIR
        pipeline.DATA_TOPICS_DIR = Path(self.tmp.name)
        self.addCleanup(setattr, pipeline, "DATA_TOPICS_DIR", self.original_dir)

        eligible = [{
            "title_hint": "치매 초기증상 건망증 차이", "seed": "치매", "seed_origin": "extra",
            "category_id": None, "source": "suggest_ko", "language": "ko",
            "domain_match": 1.0, "matched_terms": ["치매"], "score": 88, "score_breakdown": {},
        }]
        pipeline.write_queues(eligible, [], {}, eligible_top_k=5)

    def test_from_eligible_fills_seed_and_consumes(self):
        args = argparse.Namespace(from_eligible=True, seed=None)
        topic_plan.apply_from_eligible(args)
        self.assertEqual(args.seed, "치매 초기증상 건망증 차이")
        self.assertEqual(pipeline.list_eligible(), [])

    def test_explicit_seed_is_not_overridden(self):
        args = argparse.Namespace(from_eligible=True, seed="사용자 직접 입력")
        topic_plan.apply_from_eligible(args)
        self.assertEqual(args.seed, "사용자 직접 입력")
        self.assertEqual(len(pipeline.list_eligible()), 1, "seed가 이미 있으면 큐를 소비하지 않아야 한다")

    def test_flag_off_does_nothing(self):
        args = argparse.Namespace(from_eligible=False, seed=None)
        topic_plan.apply_from_eligible(args)
        self.assertIsNone(args.seed)
        self.assertEqual(len(pipeline.list_eligible()), 1)


if __name__ == "__main__":
    unittest.main()
