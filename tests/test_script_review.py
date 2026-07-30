import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dev" / "src" / "common"
sys.path.insert(0, str(SRC))

import script_review


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class ScriptReviewTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)
        (self.work_dir / "script.txt").write_text("첫 문장입니다.\n둘째 문장입니다.", encoding="utf-8")
        write_json(self.work_dir / "strategy.json", {
            "title": "텃밭만 가꿨는데",
            "main_keyword": "텃밭 가꾸기 치매",
            "hook_type": "반전형",
            "core_message": "정원 가꾸기는 뇌를 자극합니다.",
            "search_intent": "50대 이상",
        })
        write_json(self.work_dir / "script_quality.json", {
            "ok": True, "errors": [], "warnings": [],
            "metrics": {"scene_count": 10, "length_ratio": 1.1, "final_answer_present": True},
        })
        write_json(self.work_dir / "pubmed_status.json", {
            "topic": "텃밭", "pubmed_query": "gardening dementia",
            "status": "ok", "pmids": ["123"], "pmid_count": 1,
            "citations": ["Gardening and cognition, 2024"],
        })

    def tearDown(self):
        self._tmp.cleanup()

    def add_topic_plan(self):
        write_json(self.work_dir / "topic_plan.json", {
            "topic": "텃밭 가꾸기와 치매",
            "objective": {
                "type": "subscriber_growth", "decision": "selected",
                "confidence": 0.62, "base_score": 55.0, "adjusted_score": 61.2,
                "evidence_refs": ["video:abc"], "reason": "", "sync_status": "fresh-cache",
            },
            "planning": {"candidate_count": 8, "duplicates_rejected": 2, "claude_cost_usd": 0.01},
        })

    def test_bundle_includes_all_sections_when_present(self):
        self.add_topic_plan()
        bundle = script_review.build_bundle(self.work_dir)
        self.assertEqual(set(bundle["sections"]), set(script_review.SECTION_ORDER))
        self.assertEqual(bundle["missing"], [])
        self.assertEqual(bundle["sections"]["topic_plan"]["adjusted_score"], 61.2)
        self.assertEqual(bundle["sections"]["strategy"]["hook_type"], "반전형")

    def test_missing_topic_plan_is_skipped_not_an_error(self):
        # Manually-typed topics never produce topic_plan.json.
        bundle = script_review.build_bundle(self.work_dir)
        self.assertNotIn("topic_plan", bundle["sections"])
        self.assertIn("topic_plan", bundle["missing"])
        self.assertIn("script", bundle["sections"])

    def test_corrupt_artifact_is_treated_as_missing(self):
        (self.work_dir / "strategy.json").write_text("{ broken", encoding="utf-8")
        bundle = script_review.build_bundle(self.work_dir)
        self.assertIn("strategy", bundle["missing"])

    def test_empty_work_dir_yields_all_missing(self):
        with tempfile.TemporaryDirectory() as empty:
            bundle = script_review.build_bundle(empty)
            self.assertEqual(bundle["sections"], {})
            self.assertEqual(set(bundle["missing"]), set(script_review.SECTION_ORDER))

    def test_render_text_orders_sections_and_includes_script(self):
        self.add_topic_plan()
        text = script_review.render_text(script_review.build_bundle(self.work_dir))
        for title in ("주제 선정 근거", "기획 전략", "논문 근거", "대본 검증", "대본 전문"):
            self.assertIn(title, text)
        self.assertLess(text.index("주제 선정 근거"), text.index("대본 전문"))
        self.assertIn("첫 문장입니다.", text)

    def test_script_limit_truncates_only_the_script(self):
        bundle = script_review.build_bundle(self.work_dir)
        text = script_review.render_text(bundle, script_limit=5)
        self.assertIn("기획 전략", text)
        self.assertIn("...(생략)", text)
        self.assertNotIn("둘째 문장입니다.", text)

    def test_overall_limit_truncates_message(self):
        bundle = script_review.build_bundle(self.work_dir)
        text = script_review.render_text(bundle, limit=20)
        self.assertLessEqual(len(text), 20 + len("\n...(생략)"))

    def test_weak_evidence_is_flagged(self):
        write_json(self.work_dir / "pubmed_status.json", {
            "status": "no_results", "pubmed_query": "q", "pmid_count": 0,
            "message": "주제가 너무 구체적입니다.",
        })
        bundle = script_review.build_bundle(self.work_dir)
        self.assertTrue(script_review.evidence_is_weak(bundle))
        text = script_review.render_text(bundle)
        self.assertIn("PubMed 직접 검색 결과 없이", text)
        self.assertIn("주제가 너무 구체적입니다.", text)

    def test_strong_evidence_is_not_flagged(self):
        bundle = script_review.build_bundle(self.work_dir)
        self.assertFalse(script_review.evidence_is_weak(bundle))
        self.assertIn("Gardening and cognition, 2024", script_review.render_text(bundle))

    def test_validation_failure_is_flagged(self):
        write_json(self.work_dir / "script_quality.json", {
            "ok": False, "errors": ["결론 문장이 없습니다."], "warnings": [], "metrics": {},
        })
        bundle = script_review.build_bundle(self.work_dir)
        self.assertTrue(script_review.validation_failed(bundle))
        self.assertIn("결론 문장이 없습니다.", script_review.render_text(bundle))


if __name__ == "__main__":
    unittest.main()
