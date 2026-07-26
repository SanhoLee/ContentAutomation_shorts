import importlib.util
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEV_SRC = REPO_ROOT / "dev" / "src"
sys.path.insert(0, str(DEV_SRC / "instagram"))


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


card_content = load_module("instagram_card_content", DEV_SRC / "instagram" / "card_content.py")


VIDEO_META = {
    "title": "3살 이전 기억이 사라지는 진짜 이유, 당신 잘못이 아니에요",
    "frame_header": {"title": "3살 기억, 왜", "subtitle": "뇌가 한창 자라던"},
    "core_message": "부담을 줄이는 작은 습관부터 시작하세요",
    "final_answer": "3살 이전 기억이 사라지는 건 뇌의 기억 창고가 아직 완성되지 않아 빠르게 잊히는 자연스러운 과정입니다.",
}

SCENES = [
    {"text": "어릴 때 사진을 보며 왜 난 이때 기억이 없지 하신 적 있으세요?", "visual_query": "q1"},
    {"text": "3살 이전 기억이 없는 건 누구나 겪는 자연스러운 현상입니다.", "visual_query": "q2"},
    {"text": "연구에 따르면 아기들은 기억을 만들 수 있지만 나중에 꺼내기가 어려울 뿐입니다.", "visual_query": "q3"},
]


class InstagramCardContentTests(unittest.TestCase):
    def test_select_card_texts_uses_title_as_hook_and_final_answer_as_cta(self):
        texts = card_content.select_card_texts(VIDEO_META, SCENES, max_cards=8)
        self.assertEqual(texts[0], VIDEO_META["title"])
        self.assertEqual(texts[-1], VIDEO_META["final_answer"])
        self.assertIn(SCENES[0]["text"], texts)

    def test_select_card_texts_falls_back_to_frame_header_and_core_message(self):
        video_meta = {"frame_header": {"title": "제목 대체"}, "core_message": "핵심 메시지"}
        texts = card_content.select_card_texts(video_meta, SCENES, max_cards=8)
        self.assertEqual(texts[0], "제목 대체")
        self.assertEqual(texts[-1], "핵심 메시지")

    def test_select_card_texts_respects_max_cards(self):
        many_scenes = [{"text": f"장면 {i}"} for i in range(10)]
        texts = card_content.select_card_texts(VIDEO_META, many_scenes, max_cards=5)
        self.assertEqual(len(texts), 5)
        self.assertEqual(texts[0], VIDEO_META["title"])
        self.assertEqual(texts[-1], VIDEO_META["final_answer"])

    def test_truncate_for_card_collapses_whitespace_and_clips_long_text(self):
        long_text = "가" * 100
        result = card_content.truncate_for_card(long_text, max_chars=80)
        self.assertEqual(len(result), 81)  # 80 chars + ellipsis
        self.assertTrue(result.endswith("…"))

    def test_load_video_meta_and_scenes_return_defaults_when_missing(self):
        missing_dir = REPO_ROOT / "tests" / "_no_such_job_dir_"
        self.assertEqual(card_content.load_video_meta(missing_dir), {})
        self.assertEqual(card_content.load_scenes(missing_dir), [])


if __name__ == "__main__":
    unittest.main()
