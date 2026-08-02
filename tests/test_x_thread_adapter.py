import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))
sys.path.insert(0, str(ROOT / "dev" / "src" / "common" / "adapters"))

import content_package as cp
import x_thread_adapter as xta

PACKAGE = {
    "job_id": "J001",
    "hook": "치매, 원인이 여러분이 생각하는 그게 아닙니다.",
    "key_points": [
        {"text": "수면 부족이 기억력 저하와 관련있다는 연구 결과가 있습니다."},
        {"text": "매일 30분 산책이 도움이 된다는 사례가 있습니다."},
    ],
    "cta": {"action": "오늘부터 작은 습관을 시작해보세요.", "next_topic_tease": "수면과 기억력"},
    "hashtags": ["#치매예방", "#뇌건강", "#여분태그"],
}


class BuildTweetsTests(unittest.TestCase):
    def test_produces_one_tweet_per_hook_point_and_closing(self):
        tweets = xta.build_tweets(PACKAGE, ban_keywords=[])
        # hook + 2 key_points + closing = 4
        self.assertEqual(len(tweets), 4)
        self.assertIn("치매", tweets[0]["text"])
        self.assertIn("오늘부터", tweets[-1]["text"])

    def test_hashtags_only_on_last_tweet_capped_at_two(self):
        tweets = xta.build_tweets(PACKAGE, ban_keywords=[])
        for tweet in tweets[:-1]:
            self.assertNotIn("#", tweet["text"])
        hashtags_in_last = [word for word in tweets[-1]["text"].split() if word.startswith("#")]
        self.assertLessEqual(len(hashtags_in_last), 2)

    def test_number_prefix_option(self):
        tweets = xta.build_tweets(PACKAGE, number_prefix=True, ban_keywords=[])
        self.assertTrue(tweets[0]["text"].startswith("1/4"))
        self.assertTrue(tweets[-1]["text"].startswith("4/4"))

    def test_no_number_prefix_by_default(self):
        tweets = xta.build_tweets(PACKAGE, ban_keywords=[])
        self.assertFalse(tweets[0]["text"].startswith("1/"))

    def test_every_tweet_under_char_limit(self):
        long_package = dict(PACKAGE)
        long_package["key_points"] = [{"text": "아주 긴 문장입니다. " * 40}]
        tweets = xta.build_tweets(long_package, ban_keywords=[])
        for tweet in tweets:
            self.assertLessEqual(len(tweet["text"]), xta.TWEET_MAX_CHARS)

    def test_truncation_has_no_ellipsis(self):
        text = "첫 문장입니다. " * 30
        truncated = xta._truncate_at_boundary(text, 50)
        self.assertNotIn("...", truncated)
        self.assertNotIn("…", truncated)

    def test_ban_keyword_sentence_is_dropped_not_softened(self):
        risky = dict(PACKAGE)
        risky["hook"] = "이 습관으로 치매를 완치 보장합니다."
        tweets = xta.build_tweets(risky, ban_keywords=["완치", "보장"])
        self.assertFalse(any("완치" in t["text"] for t in tweets))

    def test_empty_package_yields_no_tweets(self):
        tweets = xta.build_tweets({}, ban_keywords=[])
        self.assertEqual(tweets, [])


class BuildXThreadTests(unittest.TestCase):
    def _write_job(self, job_dir, scenes):
        (job_dir / "video_meta.json").write_text(json.dumps({
            "topic": "치매 예방", "main_keyword": "치매 예방", "hook_type": "반전형",
            "title": "치매 예방 습관", "core_message": "작은 습관부터 시작하세요",
            "hashtags": "#치매예방 #뇌건강",
        }, ensure_ascii=False), encoding="utf-8")
        (job_dir / "scenes.json").write_text(json.dumps(scenes, ensure_ascii=False), encoding="utf-8")
        (job_dir / "strategy.json").write_text(json.dumps({"cta_next": "수면과 기억력"}, ensure_ascii=False), encoding="utf-8")

    def test_missing_content_package_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(xta.build_x_thread(tmp))

    def test_writes_json_and_txt_and_flips_platform_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._write_job(job_dir, [
                {"text": "훅 문장입니다.", "visual_query": "a"},
                {"text": "본문 문장입니다.", "visual_query": "b"},
                {"text": "실천 문장입니다.", "visual_query": "c"},
                {"text": "CTA 문장입니다.", "visual_query": "d"},
            ])
            cp.build_content_package(job_dir)

            payload = xta.build_x_thread(job_dir)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["method"], "rule_v1")
            self.assertEqual(len(payload["char_counts"]), len(payload["tweets"]))
            self.assertTrue((job_dir / "x_thread.json").exists())
            self.assertTrue((job_dir / "x_thread.txt").exists())

            package = cp.load_content_package(job_dir)
            self.assertTrue(package["platforms"]["x_thread"]["ready"])

    def test_no_extra_claude_calls_pure_rules(self):
        # No network/Claude client is imported anywhere in the adapter module.
        import inspect
        source = inspect.getsource(xta)
        self.assertNotIn("anthropic", source.lower())
        self.assertNotIn("api.anthropic.com", source)


if __name__ == "__main__":
    unittest.main()
