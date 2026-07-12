import importlib.util
import os
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("CAPTION_MAX_CHARS", "24")
os.environ.setdefault("CAPTION_LINE_MAX_UNITS", "13")
spec = importlib.util.spec_from_file_location("caption2", REPO_ROOT / "dev" / "src" / "2_caption.py")
caption2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(caption2)


class CaptionSegmentationTests(unittest.TestCase):
    def test_preserves_spacing_and_sentence_boundaries(self):
        text = "판단이 느려지는 게 다 이 때문입니다. 나이가 들어도 이 능력은 살아 있습니다."
        lines = caption2.split_script_to_lines(text)
        joined = " ".join(line.replace("\n", " ") for line in lines)
        self.assertIn("다 이 때문입니다.", joined)
        self.assertIn("들어도 이 능력은", joined)
        self.assertNotIn("다이", joined)
        self.assertNotIn("들어도이", joined)
        self.assertFalse(any("때문입니다. 나이가" in line.replace("\n", " ") for line in lines))

    def test_keeps_semantic_pairs_together(self):
        text = "마치 전선이 닳아서 신호가 안 가는 것처럼요. 연구에 따르면 술을 끊은 첫 4주 안에 회복이 빠릅니다."
        lines = caption2.split_script_to_lines(text)
        boundaries = " | ".join(line.replace("\n", " ") for line in lines)
        self.assertNotIn("안 | 가는", boundaries)
        self.assertNotIn("4주 | 안에", boundaries)

    def test_avoids_ending_an_event_with_a_subject_particle(self):
        lines = caption2.split_script_to_lines("신경가소성이라고 해서 뇌가 스스로 회로를 다시 잇는 힘이에요.")
        self.assertFalse(any(line.replace("\n", " ").endswith("뇌가") for line in lines[:-1]))
    def test_does_not_cross_paragraph_scene_boundaries(self):
        lines = caption2.split_script_to_lines("그 걱정은 정말 당연합니다.\n\n술은 뇌 세포 사이의 연결을 끊어놓아요.")
        self.assertFalse(any("당연합니다. 술은" in line.replace("\n", " ") for line in lines))

    def test_wraps_events_to_at_most_two_controlled_lines(self):
        lines = caption2.split_script_to_lines("두께가 두꺼워진다는 결과가 있어요. 기억하고 판단하는 힘도 빠르게 돌아와요.")
        self.assertTrue(all(line.count("\n") <= 1 for line in lines))
        self.assertTrue(all(caption2._display_units(part) <= caption2.LINE_MAX_UNITS + 1 for line in lines for part in line.split("\n")))
        self.assertFalse(any(line.strip() in {"있어요.", "돌아와요."} for line in lines))

    def test_merges_short_predicate_timing_with_incomplete_previous_caption(self):
        captions = [
            {"text": "두께가 두꺼워진다는 결과가", "start": 1.0, "end": 2.4},
            {"text": "있어요.", "start": 2.4, "end": 2.7},
        ]
        stable = caption2.stabilize_caption_durations(captions)
        self.assertEqual(len(stable), 1)
        self.assertIn("결과가 있어요.", stable[0]["text"].replace("\n", " "))
        self.assertEqual(stable[0]["end"], 2.7)


if __name__ == "__main__":
    unittest.main()