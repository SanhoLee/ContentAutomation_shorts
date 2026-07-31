import importlib.util
import os
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("CAPTION_MAX_CHARS", "24")
os.environ.setdefault("CAPTION_LINE_MAX_UNITS", "13")
# 2_caption.py imports its sibling korean_grammar, which PYTHONPATH provides in
# the pipeline (dev/config.sh) but tests have to arrange themselves.
sys.path.insert(0, str(REPO_ROOT / "dev" / "src" / "youtube"))
spec = importlib.util.spec_from_file_location("caption2", REPO_ROOT / "dev" / "src" / "youtube" / "2_caption.py")
caption2 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(caption2)


def boundaries(text):
    """Every caption break, event and line-wrap alike, as 'left ‖ right'."""
    pairs = []
    for line in caption2.split_script_to_lines(text):
        parts = line.split("\n")
        for i in range(1, len(parts)):
            pairs.append((parts[i - 1], parts[i]))
        pairs.append((parts[-1], None))
    return [(left.split()[-1], right.split()[0] if right else None) for left, right in pairs if left.split()]


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


class KoreanGrammarBoundaryTests(unittest.TestCase):
    """Cases taken from real jobs in dev/data/work that used to split badly."""

    def test_never_strands_a_dependent_noun(self):
        text = (
            "아밀로이드 단백질 축적과 신경 염증을 동시에 억제할 수 있다는 거예요. "
            "혹시 요즘 이런 적 있으셨나요? 하지만 녹차를 더하는 게 훨씬 중요합니다. "
            "그냥 초록색을 바라보는 것만으로도 뇌가 쉽니다."
        )
        stranded = [right for _, right in boundaries(text) if right]
        for word in stranded:
            self.assertFalse(
                caption2.kg.starts_with_dependent_noun(word),
                f"의존명사 {word!r}로 줄이 시작됨",
            )

    def test_keeps_auxiliary_verbs_with_their_main_verb(self):
        text = "뇌도 더 잘 깨우지 않겠냐고요. 스스로를 탓하지 않으셔도 돼요."
        for left, right in boundaries(text):
            if right is None:
                continue
            self.assertFalse(
                caption2.kg.is_auxiliary_verb(right) and not caption2.kg.ends_with_ending(left),
                f"보조용언 경계가 갈림: {left!r} ‖ {right!r}",
            )

    def test_does_not_strand_a_connective_adverb_at_the_end_of_a_line(self):
        # 그런데/사실/특히 head the next clause — they must not dangle.
        text = "커피는 뇌 건강에 좋습니다. 사실 오십 대에 커피를 줄이는 분이 많습니다."
        for left, right in boundaries(text):
            if right is None:
                continue
            self.assertNotIn(left, caption2.kg.CONNECTIVE_ADVERBS)

    def test_bare_yo_noun_does_not_end_a_sentence(self):
        self.assertFalse(caption2._is_sentence_end("중요"))
        self.assertFalse(caption2._is_sentence_end("필요"))
        self.assertTrue(caption2._is_sentence_end("중요해요"))
        self.assertTrue(caption2._is_sentence_end("좋습니다."))

    def test_soft_blocks_yield_rather_than_overflow_the_screen(self):
        # Every boundary here is soft-blocked; refusing them all would push the
        # line past the screen, so one has to give.
        wrapped = caption2._wrap_caption("그런데 여기서 중요한 게 있어요.".split())
        for part in wrapped.split("\n"):
            self.assertLessEqual(caption2._display_units(part), caption2.LINE_MAX_UNITS + 1)

    def test_hard_blocks_outrank_a_clean_ending(self):
        # 억제할 ends in a modifier ending, but 수 cannot start a line.
        self.assertEqual(caption2._boundary_block("억제할", "수"), caption2.BLOCK_HARD)
        # 있는 only looks like an auxiliary; a real ending wins over the soft rule.
        self.assertEqual(caption2._boundary_block("좋습니다", "있는"), caption2.BLOCK_NONE)

    def test_does_not_split_a_compound_term(self):
        tokens = "이건 경도 인지 장애 신호입니다".split()
        self.assertEqual(caption2._block_level(tokens, 2), caption2.BLOCK_HARD)
        self.assertEqual(caption2._block_level(tokens, 3), caption2.BLOCK_HARD)


if __name__ == "__main__":
    unittest.main()