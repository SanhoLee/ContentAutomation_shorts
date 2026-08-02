import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
    """humanize=False everywhere here: these test the rule-based path in
    isolation, and must stay deterministic/network-free even if a
    developer's shell happens to export ANTHROPIC_API_KEY."""

    def test_packs_short_sentences_into_fewer_tweets(self):
        # hook + 2 key_points + closing are all short enough that the first
        # three pack into one tweet under TWEET_MAX_CHARS(139); only the
        # closing (which also carries hashtags) needs its own tweet.
        tweets = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        self.assertEqual(len(tweets), 2)
        self.assertIn("치매", tweets[0]["text"])
        self.assertIn("수면 부족", tweets[0]["text"])
        self.assertIn("산책", tweets[0]["text"])
        self.assertIn("오늘부터", tweets[-1]["text"])

    def test_long_sentence_starts_its_own_tweet_instead_of_packing(self):
        # A sentence that alone is close to the limit shouldn't be crammed
        # in next to the hook -- it should start a fresh tweet.
        package = dict(PACKAGE)
        package["key_points"] = [{"text": "아주 " * 60 + "긴 문장입니다."}]
        tweets = xta.build_tweets(package, ban_keywords=[], humanize=False)
        self.assertGreaterEqual(len(tweets), 2)
        self.assertNotIn("아주 아주", tweets[0]["text"])

    def test_hashtags_only_on_last_tweet_capped_at_two(self):
        tweets = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        for tweet in tweets[:-1]:
            self.assertNotIn("#", tweet["text"])
        hashtags_in_last = [word for word in tweets[-1]["text"].split() if word.startswith("#")]
        self.assertLessEqual(len(hashtags_in_last), 2)

    def test_number_prefix_option(self):
        tweets = xta.build_tweets(PACKAGE, number_prefix=True, ban_keywords=[], humanize=False)
        total = len(tweets)
        self.assertTrue(tweets[0]["text"].startswith(f"1/{total}"))
        self.assertTrue(tweets[-1]["text"].startswith(f"{total}/{total}"))

    def test_no_number_prefix_by_default(self):
        tweets = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        self.assertFalse(tweets[0]["text"].startswith("1/"))

    def test_every_tweet_under_char_limit(self):
        long_package = dict(PACKAGE)
        long_package["key_points"] = [{"text": "아주 긴 문장입니다. " * 40}]
        tweets = xta.build_tweets(long_package, ban_keywords=[], humanize=False)
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
        tweets = xta.build_tweets(risky, ban_keywords=["완치", "보장"], humanize=False)
        self.assertFalse(any("완치" in t["text"] for t in tweets))

    def test_empty_package_yields_no_tweets(self):
        tweets = xta.build_tweets({}, ban_keywords=[], humanize=False)
        self.assertEqual(tweets, [])


class HumanizeTests(unittest.TestCase):
    """The casual-tone rewrite pass: opt-in by default, but must never make
    a network call or change output when ANTHROPIC_API_KEY is unset, and
    must fall back cleanly on any failure (bad JSON, count mismatch, a
    rewrite that reintroduces a banned word)."""

    def setUp(self):
        self._env_patch = mock.patch.dict(os.environ, {}, clear=False)
        self._env_patch.start()
        for key in ("ANTHROPIC_API_KEY", "X_THREAD_HUMANIZE"):
            os.environ.pop(key, None)
        # Hermetic: real budget DB has no bearing on whether the rewrite
        # code path is exercised correctly in these tests.
        self._budget_patch = mock.patch("claude_cost.assert_budget", return_value=None)
        self._budget_patch.start()
        self._usage_patch = mock.patch("claude_cost.record_usage", return_value=None)
        self._usage_patch.start()

    def tearDown(self):
        self._env_patch.stop()
        self._budget_patch.stop()
        self._usage_patch.stop()

    def test_no_api_key_falls_back_without_network_call(self):
        with mock.patch("requests.post", side_effect=AssertionError("should not call network")):
            tweets = xta.build_tweets(PACKAGE, ban_keywords=[])  # humanize=None -> default True
        baseline = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        self.assertEqual([t["text"] for t in tweets], [t["text"] for t in baseline])

    def test_env_var_off_disables_rewrite(self):
        os.environ["X_THREAD_HUMANIZE"] = "0"
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        with mock.patch.object(xta, "_humanize_texts_with_claude") as rewrite:
            xta.build_tweets(PACKAGE, ban_keywords=[])
            rewrite.assert_not_called()

    def test_successful_rewrite_replaces_raw_text(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        casual = ["치매 원인 그거 아니라던데?", "잠 부족하면 기억력 떨어진대", "산책 30분이면 된대", "오늘부터 해보자! 다음엔 수면 얘기도 궁금하지?"]
        with mock.patch.object(xta, "_humanize_texts_with_claude", return_value=casual):
            tweets = xta.build_tweets(PACKAGE, ban_keywords=[])
        self.assertIn("치매 원인 그거 아니라던데?", tweets[0]["text"])

    def test_rewrite_reintroducing_ban_keyword_is_discarded_wholesale(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        bad_rewrite = ["완치 보장 가능", "잠 부족하면 기억력 떨어진대", "산책 30분이면 된대", "오늘부터 해보자!"]
        with mock.patch.object(xta, "_humanize_texts_with_claude", return_value=bad_rewrite):
            tweets = xta.build_tweets(PACKAGE, ban_keywords=["완치", "보장"])
        baseline = xta.build_tweets(PACKAGE, ban_keywords=["완치", "보장"], humanize=False)
        self.assertEqual([t["text"] for t in tweets], [t["text"] for t in baseline])

    def test_malformed_claude_response_falls_back(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        response = mock.Mock()
        response.raise_for_status = mock.Mock()
        response.json.return_value = {"content": [{"type": "text", "text": "이건 JSON이 아닙니다"}]}
        with mock.patch("requests.post", return_value=response):
            tweets = xta.build_tweets(PACKAGE, ban_keywords=[])
        baseline = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        self.assertEqual([t["text"] for t in tweets], [t["text"] for t in baseline])


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

            payload = xta.build_x_thread(job_dir, humanize=False)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["method"], "rule_v1")
            self.assertEqual(len(payload["char_counts"]), len(payload["tweets"]))
            self.assertTrue((job_dir / "x_thread.json").exists())
            self.assertTrue((job_dir / "x_thread.txt").exists())

            package = cp.load_content_package(job_dir)
            self.assertTrue(package["platforms"]["x_thread"]["ready"])

    def test_rebuild_refuses_to_overwrite_an_already_posted_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._write_job(job_dir, [
                {"text": "훅 문장입니다.", "visual_query": "a"},
                {"text": "본문 문장입니다.", "visual_query": "b"},
            ])
            cp.build_content_package(job_dir)
            xta.build_x_thread(job_dir, humanize=False)

            posted_path = job_dir / "x_thread.json"
            posted = json.loads(posted_path.read_text(encoding="utf-8"))
            posted["posted"] = True
            posted["posted_at"] = "2026-01-01T00:00:00+00:00"
            posted["tweet_ids"] = ["111", "222"]
            posted_path.write_text(json.dumps(posted, ensure_ascii=False), encoding="utf-8")

            result = xta.build_x_thread(job_dir, humanize=False)
            self.assertEqual(result["tweet_ids"], ["111", "222"])
            self.assertTrue(result["posted"])

            on_disk = json.loads(posted_path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["tweet_ids"], ["111", "222"])

    def test_no_api_key_end_to_end_falls_back_to_rules(self):
        # Default (humanize=None) path with no ANTHROPIC_API_KEY set must
        # behave identically to humanize=False -- no crash, no network call.
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._write_job(job_dir, [
                {"text": "훅 문장입니다.", "visual_query": "a"},
                {"text": "본문 문장입니다.", "visual_query": "b"},
            ])
            cp.build_content_package(job_dir)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ANTHROPIC_API_KEY", None)
                with mock.patch("requests.post", side_effect=AssertionError("should not call network")):
                    payload = xta.build_x_thread(job_dir)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["method"], "rule_v1")


if __name__ == "__main__":
    unittest.main()
