import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

import topic_candidate_pipeline as pipeline

VOCAB = ("치매", "기억", "뇌", "수면", "운동", "예방")

RULES = {
    "threshold": 60,
    "eligible_top_k": 10,
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

# Deliberately varied wording so score_candidate spreads them out and the
# ordering assertions below test real ranking, not insertion order.
TITLES = [
    "치매 초기증상 건망증 차이는 무엇일까",
    "수면 부족과 기억력 저하 연구 결과",
    "치매 예방 운동 방법 정리",
    "뇌 건강 식단 인지 기능 연구",
    "기억력 감퇴 신호 언제부터 주의할까",
]


def _seed_queue(base_dir, titles=None):
    """Write a real eligible queue through the production write path."""
    candidates = [
        {"title_hint": title, "seed": "치매", "duplicate": False}
        for title in (titles if titles is not None else TITLES)
    ]
    eligible, rejected = pipeline.score_and_rank(
        candidates, vocabulary=VOCAB, recent_titles=[], rules=RULES,
    )
    pipeline.write_queues(eligible, rejected, {}, base_dir=base_dir, eligible_top_k=10)
    return eligible


def _read_eligible(base_dir, topic_id):
    path = Path(base_dir) / "eligible" / f"{topic_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


class TopCandidatesTests(unittest.TestCase):
    def test_returns_top_three_in_descending_score_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_queue(tmp)
            top = pipeline.top_candidates(3, base_dir=tmp)
            self.assertEqual(len(top), 3)
            scores = [item["score"] for item in top]
            self.assertEqual(scores, sorted(scores, reverse=True))

    def test_default_n_is_three(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_queue(tmp)
            self.assertEqual(len(pipeline.top_candidates(base_dir=tmp)), pipeline.DEFAULT_TOP_N)

    def test_consumed_candidates_are_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_queue(tmp)
            before = pipeline.top_candidates(3, base_dir=tmp)
            consumed_id = before[0]["topic_id"]
            payload = _read_eligible(tmp, consumed_id)
            payload["consumed"] = True
            (Path(tmp) / "eligible" / f"{consumed_id}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8",
            )

            after = pipeline.top_candidates(3, base_dir=tmp)
            self.assertNotIn(consumed_id, [item["topic_id"] for item in after])

    def test_empty_queue_returns_empty_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(pipeline.top_candidates(3, base_dir=tmp), [])


class SelectTopicTests(unittest.TestCase):
    def test_marks_status_selected_and_stamps_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_queue(tmp)
            target = pipeline.top_candidates(3, base_dir=tmp)[0]

            selected = pipeline.select_topic(target["topic_id"], base_dir=tmp)
            self.assertIsNotNone(selected)
            self.assertEqual(selected["status"], pipeline.STATUS_SELECTED)
            self.assertTrue(selected["selected_at"])

            on_disk = _read_eligible(tmp, target["topic_id"])
            self.assertEqual(on_disk["status"], pipeline.STATUS_SELECTED)

    def test_does_not_touch_consumed_flag(self):
        # "selected" (a human picked it) and "consumed" (the pipeline spent it)
        # are separate states; conflating them would break pick_top_eligible.
        with tempfile.TemporaryDirectory() as tmp:
            _seed_queue(tmp)
            target = pipeline.top_candidates(3, base_dir=tmp)[0]
            pipeline.select_topic(target["topic_id"], base_dir=tmp)
            self.assertFalse(_read_eligible(tmp, target["topic_id"])["consumed"])

    def test_selected_candidate_drops_out_and_next_one_moves_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_queue(tmp)
            before = pipeline.top_candidates(3, base_dir=tmp)
            pipeline.select_topic(before[0]["topic_id"], base_dir=tmp)

            after = pipeline.top_candidates(3, base_dir=tmp)
            self.assertNotIn(before[0]["topic_id"], [item["topic_id"] for item in after])
            self.assertEqual(after[0]["topic_id"], before[1]["topic_id"])

    def test_writes_selected_pointer_that_current_selection_reads_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_queue(tmp)
            target = pipeline.top_candidates(3, base_dir=tmp)[0]
            pipeline.select_topic(target["topic_id"], base_dir=tmp)

            self.assertTrue((Path(tmp) / pipeline.SELECTED_FILENAME).exists())
            selection = pipeline.current_selection(base_dir=tmp)
            self.assertEqual(selection["topic_id"], target["topic_id"])
            self.assertEqual(selection["title_hint"], target["title_hint"])

    def test_unknown_topic_id_returns_none_instead_of_raising(self):
        # A stale Slack button points at a topic_id the queue no longer has;
        # that must not crash the bot.
        with tempfile.TemporaryDirectory() as tmp:
            _seed_queue(tmp)
            self.assertIsNone(pipeline.select_topic("2020-01-01_99_없는주제", base_dir=tmp))

    def test_selecting_twice_keeps_the_latest_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_queue(tmp)
            top = pipeline.top_candidates(3, base_dir=tmp)
            pipeline.select_topic(top[0]["topic_id"], base_dir=tmp)
            pipeline.select_topic(top[1]["topic_id"], base_dir=tmp)
            self.assertEqual(
                pipeline.current_selection(base_dir=tmp)["topic_id"], top[1]["topic_id"],
            )


class CurrentSelectionTests(unittest.TestCase):
    def test_no_selection_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(pipeline.current_selection(base_dir=tmp))

    def test_corrupt_pointer_returns_none_instead_of_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / pipeline.SELECTED_FILENAME).write_text("{not json", encoding="utf-8")
            self.assertIsNone(pipeline.current_selection(base_dir=tmp))


class RenderCandidateLinesTests(unittest.TestCase):
    def test_lines_carry_rank_title_score_and_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_queue(tmp)
            top = pipeline.top_candidates(3, base_dir=tmp)
            lines = pipeline.render_candidate_lines(top)

            self.assertEqual(len(lines), 3)
            self.assertTrue(lines[0].startswith("1. "))
            self.assertIn(top[0]["title_hint"], lines[0])
            self.assertIn(f"점수 {top[0]['score']}", lines[0])
            self.assertIn("시드 치매", lines[0])

    def test_score_reasons_use_korean_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            _seed_queue(tmp)
            line = pipeline.render_candidate_lines(pipeline.top_candidates(1, base_dir=tmp))[0]
            self.assertTrue(any(label in line for label in pipeline.SCORE_LABELS.values()))

    def test_empty_input_yields_no_lines(self):
        self.assertEqual(pipeline.render_candidate_lines([]), [])


if __name__ == "__main__":
    unittest.main()
