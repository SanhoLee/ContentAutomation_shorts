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
    "source": {"topic": "치매 예방", "title": "치매를 부르는 습관"},
    "story_type": "habit_mechanism",
    "core_message": "작은 습관이 뇌를 지킵니다",
    "hook": "치매, 원인이 여러분이 생각하는 그게 아닙니다.",
    "key_points": [
        {"text": "수면 부족이 기억력 저하와 관련있다는 연구 결과가 있습니다."},
        {"text": "매일 30분 산책이 도움이 된다는 사례가 있습니다."},
    ],
    # Kept deliberately: the CTA scene and its tease must be ignored, not
    # merely absent from the fixture.
    "cta": {"action": "오늘부터 작은 습관을 시작해보세요.", "next_topic_tease": "수면과 기억력"},
    "hashtags": ["#치매예방", "#뇌건강", "#여분태그"],
}


def _body(tweets):
    """Lead tweet text with the X title line stripped off, so a test can
    assert on the packed body without caring what the title says."""
    text = tweets[0]["text"]
    return text.split(xta.TITLE_SEPARATOR, 1)[-1] if xta.TITLE_SEPARATOR in text else text


class BuildTweetsTests(unittest.TestCase):
    """humanize=False everywhere here: these test the rule-based path in
    isolation, and must stay deterministic/network-free even if a
    developer's shell happens to export ANTHROPIC_API_KEY."""

    def test_packs_short_sentences_into_fewer_tweets(self):
        # hook + 2 key_points are all short enough to pack into one tweet
        # under TWEET_MAX_CHARS(139) minus the title's reserved width; the
        # recap then closes the thread as its own tweet.
        tweets = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        self.assertEqual(len(tweets), 2)
        self.assertIn("치매", tweets[0]["text"])
        self.assertIn("수면 부족", tweets[0]["text"])
        self.assertIn("산책", tweets[0]["text"])

    def test_long_sentence_starts_its_own_tweet_instead_of_packing(self):
        # A sentence that alone is close to the limit shouldn't be crammed
        # in next to the hook -- it should start a fresh tweet.
        package = dict(PACKAGE)
        package["key_points"] = [{"text": "아주 " * 60 + "긴 문장입니다."}]
        tweets = xta.build_tweets(package, ban_keywords=[], humanize=False)
        self.assertGreaterEqual(len(tweets), 2)
        self.assertNotIn("아주 아주", tweets[0]["text"])

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

    def test_lead_tweet_with_title_still_fits_with_number_prefix(self):
        # Worst case for the lead tweet: it carries both the numeric prefix
        # and the title on top of its packed body.
        package = dict(PACKAGE)
        package["source"] = {"topic": "치매 예방", "title": "긴 제목입니다 " * 12}
        tweets = xta.build_tweets(package, number_prefix=True, ban_keywords=[], humanize=False)
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

    def test_title_alone_never_produces_a_tweet(self):
        # A package with a title but no usable body must stay empty rather
        # than post a title-only tweet.
        tweets = xta.build_tweets(
            {"source": {"title": "제목만 있습니다"}}, ban_keywords=[], humanize=False,
        )
        self.assertEqual(tweets, [])


class LeadTitleTests(unittest.TestCase):
    """The lead tweet carries an X-optimized title above the first packed
    group. It may differ from the Shorts title, but must never push the
    lead tweet over the char limit or appear on a later tweet."""

    def test_lead_tweet_starts_with_the_title_line(self):
        tweets = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        self.assertTrue(tweets[0]["text"].startswith("치매를 부르는 습관"))
        self.assertIn(xta.TITLE_SEPARATOR, tweets[0]["text"])

    def test_first_message_content_still_rides_in_the_lead_tweet(self):
        tweets = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        self.assertIn("치매, 원인이", _body(tweets))

    def test_title_only_appears_on_the_lead_tweet(self):
        tweets = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        for tweet in tweets[1:]:
            self.assertNotIn("치매를 부르는 습관", tweet["text"])

    def test_title_falls_back_to_core_message_then_topic(self):
        package = dict(PACKAGE)
        package["source"] = {"topic": "치매 예방"}
        package["core_message"] = "작은 습관부터 시작하세요"
        tweets = xta.build_tweets(package, ban_keywords=[], humanize=False)
        self.assertTrue(tweets[0]["text"].startswith("작은 습관부터 시작하세요"))

        package["core_message"] = ""
        tweets = xta.build_tweets(package, ban_keywords=[], humanize=False)
        self.assertTrue(tweets[0]["text"].startswith("치매 예방"))

    def test_long_title_is_capped(self):
        package = dict(PACKAGE)
        package["source"] = {"title": "가" * 200}
        tweets = xta.build_tweets(package, ban_keywords=[], humanize=False)
        title = tweets[0]["text"].split(xta.TITLE_SEPARATOR, 1)[0]
        self.assertLessEqual(len(title), xta.X_TITLE_MAX_CHARS)

    def test_ban_keyword_title_is_dropped(self):
        package = dict(PACKAGE)
        package["source"] = {"title": "치매 완치 보장"}
        package["core_message"] = ""
        package["topic"] = ""
        tweets = xta.build_tweets(package, ban_keywords=["완치", "보장"], humanize=False)
        self.assertNotIn("완치", tweets[0]["text"])


class GrammarWarningTests(unittest.TestCase):
    """Advisory only -- these never change the tweets, they just give the
    approval screen something to point at."""

    def test_misplaced_negation_is_reported_with_its_tweet_number(self):
        payload = {"tweets": [
            {"index": 1, "text": "일이 안 손에 잡힐 땐 의지 문제가 아니에요"},
            {"index": 2, "text": "잠이 부족하면 기억이 정리되지 않아요"},
        ]}
        warnings = xta.grammar_warnings(payload)
        self.assertEqual(len(warnings), 1)
        self.assertIn("1번 트윗", warnings[0])
        self.assertIn("안 손에", warnings[0])

    def test_clean_thread_produces_no_warnings(self):
        payload = {"tweets": [{"index": 1, "text": "일이 손에 안 잡힐 땐 뇌 문제예요"}]}
        self.assertEqual(xta.grammar_warnings(payload), [])

    def test_empty_payload_is_safe(self):
        self.assertEqual(xta.grammar_warnings({}), [])

    def test_warnings_do_not_alter_the_tweets(self):
        tweets = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        before = [t["text"] for t in tweets]
        xta.grammar_warnings({"tweets": tweets})
        self.assertEqual([t["text"] for t in tweets], before)


class SummaryTweetTests(unittest.TestCase):
    """The thread closes with a recap, not the script's CTA scene. Asking a
    reader who just finished the thread to go do one more thing reads as a
    Shorts outro bolted onto X."""

    def test_cta_and_next_episode_tease_never_reach_the_thread(self):
        tweets = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        joined = " ".join(t["text"] for t in tweets)
        self.assertNotIn("오늘부터", joined)
        self.assertNotIn("다음 편에서는", joined)
        self.assertNotIn("궁금하지 않으세요", joined)

    def test_last_tweet_is_the_recap(self):
        tweets = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        self.assertIn("작은 습관이 뇌를 지킵니다", tweets[-1]["text"])

    def test_recap_is_its_own_tweet_not_packed_with_the_body(self):
        tweets = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        self.assertGreaterEqual(len(tweets), 2)
        self.assertNotIn("작은 습관이 뇌를 지킵니다", tweets[0]["text"])

    def test_claude_summary_lines_are_bulleted_when_there_are_several(self):
        rewrite = {
            "title": "", "texts": ["가", "나", "다"], "summary_lead": "정리하면",
            "summary": ["잠이 먼저입니다", "걷기가 다음입니다", "오래 가는 건 습관입니다"],
        }
        with mock.patch.object(xta, "_humanize_with_claude", return_value=rewrite):
            tweets = xta.build_tweets(PACKAGE, ban_keywords=[])
        recap = tweets[-1]["text"]
        self.assertEqual(recap.count(xta.SUMMARY_BULLET), 3)
        self.assertIn("걷기가 다음입니다", recap)

    def test_single_recap_line_gets_no_bullet(self):
        tweets = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        self.assertNotIn(xta.SUMMARY_BULLET, tweets[-1]["text"])

    def test_recap_falls_back_to_core_message_without_claude(self):
        package = dict(PACKAGE)
        package["core_message"] = "핵심은 수면입니다"
        tweets = xta.build_tweets(package, ban_keywords=[], humanize=False)
        self.assertIn("핵심은 수면입니다", tweets[-1]["text"])

    def test_no_core_message_and_no_claude_summary_means_no_recap_tweet(self):
        package = dict(PACKAGE)
        package["core_message"] = ""
        tweets = xta.build_tweets(package, ban_keywords=[], humanize=False)
        self.assertEqual(len(tweets), 1)
        self.assertIn("치매, 원인이", tweets[0]["text"])

    def test_ban_keyword_recap_line_is_dropped(self):
        package = dict(PACKAGE)
        package["core_message"] = "완치를 보장합니다"
        tweets = xta.build_tweets(package, ban_keywords=["완치", "보장"], humanize=False)
        joined = " ".join(t["text"] for t in tweets)
        self.assertNotIn("완치", joined)
        # Nothing usable left for the recap, so the thread just ends on the body.
        self.assertEqual(len(tweets), 1)

    def test_recap_respects_the_char_budget_with_a_number_prefix(self):
        rewrite = {
            "title": "", "texts": ["가", "나", "다"], "summary_lead": "가" * 40,
            "summary": ["나" * 60, "다" * 60, "라" * 60],
        }
        with mock.patch.object(xta, "_humanize_with_claude", return_value=rewrite):
            tweets = xta.build_tweets(PACKAGE, number_prefix=True, ban_keywords=[])
        for tweet in tweets:
            self.assertLessEqual(len(tweet["text"]), xta.TWEET_MAX_CHARS)

    def test_empty_package_still_yields_no_recap_only_tweet(self):
        self.assertEqual(
            xta.build_tweets({"core_message": "핵심입니다"}, ban_keywords=[], humanize=False), [],
        )


class SummaryLeadTests(unittest.TestCase):
    """The recap's lead-in must not be one hardcoded phrase -- that is its
    own robot tell. It varies per job (seeded, so a re-run reproduces it)
    and is bucketed by story_type."""

    def _lead(self, package, job_id):
        tweets = xta.build_tweets(package, ban_keywords=[], humanize=False, job_id=job_id)
        return tweets[-1]["text"].split("\n", 1)[0]

    def test_lead_comes_from_the_matching_story_type_bucket(self):
        pool = xta._load_summary_phrases()["by_story_type"]["habit_mechanism"]
        self.assertIn(self._lead(PACKAGE, "J0001"), pool)

    def test_same_job_id_always_produces_the_same_lead(self):
        self.assertEqual(self._lead(PACKAGE, "J0007"), self._lead(PACKAGE, "J0007"))

    def test_different_jobs_do_not_all_get_the_same_lead(self):
        leads = {self._lead(PACKAGE, f"J{i:04d}") for i in range(30)}
        self.assertGreater(len(leads), 1)

    def test_unknown_story_type_falls_back_to_the_default_bucket(self):
        package = dict(PACKAGE)
        package["story_type"] = "does_not_exist"
        self.assertIn(self._lead(package, "J0001"), xta._load_summary_phrases()["default"])

    def test_missing_story_type_falls_back_to_the_default_bucket(self):
        package = dict(PACKAGE)
        package["story_type"] = ""
        self.assertIn(self._lead(package, "J0001"), xta._load_summary_phrases()["default"])

    def test_claude_lead_wins_over_the_pool(self):
        rewrite = {
            "title": "", "texts": ["가", "나", "다"],
            "summary_lead": "이번 편은 이렇게 정리돼요", "summary": ["핵심입니다"],
        }
        with mock.patch.object(xta, "_humanize_with_claude", return_value=rewrite):
            tweets = xta.build_tweets(PACKAGE, ban_keywords=[])
        self.assertTrue(tweets[-1]["text"].startswith("이번 편은 이렇게 정리돼요"))

    def test_phrase_file_obeys_its_own_rules(self):
        phrases = xta._load_summary_phrases()
        buckets = [("default", phrases["default"])] + list(phrases["by_story_type"].items())
        self.assertTrue(buckets)
        for name, pool in buckets:
            self.assertTrue(pool, f"{name} 버킷이 비어 있습니다")
            for phrase in pool:
                self.assertLessEqual(len(phrase), xta.SUMMARY_LEAD_MAX_CHARS, phrase)
                self.assertNotIn("#", phrase)
                self.assertNotIn("http", phrase)

    def test_unreadable_phrase_file_yields_a_recap_without_a_lead(self):
        missing = Path(tempfile.gettempdir()) / "x_thread_phrases_does_not_exist.json"
        with mock.patch.object(xta, "_summary_phrases_cache", None), \
             mock.patch.object(xta, "SUMMARY_PHRASES_PATH", missing):
            tweets = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        self.assertEqual(tweets[-1]["text"], "작은 습관이 뇌를 지킵니다")


class NoHashtagsOrLinksTests(unittest.TestCase):
    """Hashtags and URLs are banned from thread text outright -- both cost
    reach on X, and neither may survive the rule-based path or a rewrite."""

    def test_no_hashtags_anywhere_even_though_the_package_has_them(self):
        tweets = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        for tweet in tweets:
            self.assertNotIn("#", tweet["text"])

    def test_hashtags_inside_source_text_are_stripped(self):
        package = dict(PACKAGE)
        package["key_points"] = [{"text": "산책이 도움이 된다는 사례가 있습니다 #뇌건강 #치매예방"}]
        tweets = xta.build_tweets(package, ban_keywords=[], humanize=False)
        for tweet in tweets:
            self.assertNotIn("#", tweet["text"])
        self.assertIn("산책이 도움이 된다는 사례가 있습니다", tweets[0]["text"])

    def test_urls_inside_source_text_are_stripped(self):
        package = dict(PACKAGE)
        package["hook"] = "자세한 건 https://example.com/study 를 보세요."
        tweets = xta.build_tweets(package, ban_keywords=[], humanize=False)
        for tweet in tweets:
            self.assertNotIn("http", tweet["text"])
            self.assertNotIn("www.", tweet["text"])

    def test_rewrite_that_adds_a_hashtag_or_link_is_sanitized(self):
        rewrite = {
            "title": "잠 못 자면 기억력부터 흔들려요 #뇌건강",
            "texts": [
                "치매 원인, 생각하시는 그게 아닐 수 있어요. https://x.com/foo",
                "잠이 부족하면 기억력이 떨어진다는 연구가 있어요.",
                "하루 30분 걷기만 해도 도움이 된대요.",
            ],
            "summary_lead": "정리해보면 #요약",
            "summary": ["작은 습관이 뇌를 지켜요 https://example.com"],
        }
        with mock.patch.object(xta, "_humanize_with_claude", return_value=rewrite):
            tweets = xta.build_tweets(PACKAGE, ban_keywords=[])
        for tweet in tweets:
            self.assertNotIn("#", tweet["text"])
            self.assertNotIn("http", tweet["text"])
        self.assertTrue(tweets[0]["text"].startswith("잠 못 자면 기억력부터 흔들려요"))


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

    def test_prompt_targets_20s_to_40s_in_polite_korean(self):
        self.assertIn("20~40대", xta.HUMANIZE_PROMPT)
        self.assertIn("존댓말", xta.HUMANIZE_PROMPT)
        self.assertNotIn("반말 섞인", xta.HUMANIZE_PROMPT)

    def test_no_api_key_falls_back_without_network_call(self):
        with mock.patch("requests.post", side_effect=AssertionError("should not call network")):
            tweets = xta.build_tweets(PACKAGE, ban_keywords=[])  # humanize=None -> default True
        baseline = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        self.assertEqual([t["text"] for t in tweets], [t["text"] for t in baseline])

    def test_env_var_off_disables_rewrite(self):
        os.environ["X_THREAD_HUMANIZE"] = "0"
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        with mock.patch.object(xta, "_humanize_with_claude") as rewrite:
            xta.build_tweets(PACKAGE, ban_keywords=[])
            rewrite.assert_not_called()

    def test_successful_rewrite_replaces_raw_text_and_title(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        rewrite = {
            "title": "치매 원인, 생각하시는 그게 아닙니다",
            "texts": [
                "치매 원인, 그거 아니라고 하네요.",
                "잠이 부족하면 기억력이 떨어진대요.",
                "산책 30분이면 충분하다고 해요.",
            ],
            "summary_lead": "짧게 정리해드리면",
            "summary": ["잠과 걷기, 이 둘이 핵심이에요"],
        }
        with mock.patch.object(xta, "_humanize_with_claude", return_value=rewrite):
            tweets = xta.build_tweets(PACKAGE, ban_keywords=[])
        self.assertTrue(tweets[0]["text"].startswith("치매 원인, 생각하시는 그게 아닙니다"))
        self.assertIn("치매 원인, 그거 아니라고 하네요.", _body(tweets))
        self.assertTrue(tweets[-1]["text"].startswith("짧게 정리해드리면"))
        self.assertIn("잠과 걷기, 이 둘이 핵심이에요", tweets[-1]["text"])

    def test_rewrite_without_a_title_keeps_the_rule_based_one(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        rewrite = {"title": "", "texts": ["가", "나", "다"], "summary_lead": "", "summary": []}
        with mock.patch.object(xta, "_humanize_with_claude", return_value=rewrite):
            tweets = xta.build_tweets(PACKAGE, ban_keywords=[])
        self.assertTrue(tweets[0]["text"].startswith("치매를 부르는 습관"))

    def test_rewrite_reintroducing_ban_keyword_is_discarded_wholesale(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        bad_rewrite = {
            "title": "완치 보장",
            "texts": ["완치 보장 가능", "잠 부족하면 기억력 떨어져요", "산책 30분이면 돼요"],
            "summary_lead": "완치 정리하면",
            "summary": ["완치 보장됩니다"],
        }
        with mock.patch.object(xta, "_humanize_with_claude", return_value=bad_rewrite):
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

    def test_sentence_count_mismatch_falls_back(self):
        os.environ["ANTHROPIC_API_KEY"] = "test-key"
        response = mock.Mock()
        response.raise_for_status = mock.Mock()
        response.json.return_value = {"content": [{
            "type": "text",
            "text": json.dumps({"title": "제목", "sentences": ["한 문장뿐"]}, ensure_ascii=False),
        }]}
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
            self.assertEqual(payload["method"], "rule_v3")
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

    def test_save_x_thread_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            xta.save_x_thread(job_dir, {"job_id": "J1", "sources_dm_sent": True})
            self.assertTrue(xta.load_x_thread(job_dir)["sources_dm_sent"])

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
            self.assertEqual(payload["method"], "rule_v3")


class SourcesTests(unittest.TestCase):
    """Sources are no longer a tweet: they go into the payload as
    `sources_text` for the bots to DM, so they may keep their links."""

    def test_pubmed_citations_never_become_a_tweet(self):
        citations = [{"pmid": "12345678", "journal": "Neurology", "year": "2022"}]
        text = xta.build_sources_text(PACKAGE, citations, [])
        self.assertIn(xta.SOURCES_LABEL, text)
        self.assertIn("https://pubmed.ncbi.nlm.nih.gov/12345678/", text)

        tweets = xta.build_tweets(PACKAGE, ban_keywords=[], humanize=False)
        for tweet in tweets:
            self.assertNotIn("pubmed", tweet["text"])
            self.assertNotIn(xta.SOURCES_LABEL, tweet["text"])

    def test_no_citations_or_evidence_yields_empty_sources_text(self):
        self.assertEqual(xta.build_sources_text(PACKAGE, [], []), "")

    def test_evidence_source_hint_used_when_no_pmids(self):
        package = dict(PACKAGE)
        package["evidence"] = [{"claim": "수면과 기억력", "source_hint": "2019년 국내 코호트 연구"}]
        self.assertIn("2019년 국내 코호트 연구", xta.build_sources_text(package, [], []))

    def test_pmids_take_priority_over_evidence_hint(self):
        package = dict(PACKAGE)
        package["evidence"] = [{"claim": "x", "source_hint": "근거 힌트 텍스트"}]
        text = xta.build_sources_text(package, [{"pmid": "1", "journal": "J", "year": "2021"}], [])
        self.assertIn("pubmed.ncbi.nlm.nih.gov", text)
        self.assertNotIn("근거 힌트 텍스트", text)

    def test_sources_capped_at_max_source_links(self):
        citations = [{"pmid": str(i), "journal": "Journal", "year": "2020"} for i in range(10)]
        text = xta.build_sources_text(PACKAGE, citations, [])
        self.assertLessEqual(text.count("pubmed.ncbi.nlm.nih.gov"), xta.MAX_SOURCE_LINKS)

    def test_ban_keyword_drops_the_source_line(self):
        citations = [{"pmid": "1", "journal": "완치 저널", "year": "2020"}]
        self.assertEqual(xta.build_sources_text(PACKAGE, citations, ["완치"]), "")

    def test_pubmed_status_citations_land_in_the_payload_not_the_tweets(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            (job_dir / "video_meta.json").write_text(json.dumps({
                "topic": "치매 예방", "title": "치매 예방 습관", "core_message": "작은 습관부터",
                "hashtags": "#치매예방",
            }, ensure_ascii=False), encoding="utf-8")
            (job_dir / "scenes.json").write_text(json.dumps([
                {"text": "훅 문장입니다.", "visual_query": "a"},
                {"text": "CTA 문장입니다.", "visual_query": "d"},
            ], ensure_ascii=False), encoding="utf-8")
            (job_dir / "strategy.json").write_text(json.dumps({}, ensure_ascii=False), encoding="utf-8")
            (job_dir / "pubmed_status.json").write_text(json.dumps({
                "citations": [{"pmid": "999", "journal": "J", "year": "2023"}],
            }, ensure_ascii=False), encoding="utf-8")
            cp.build_content_package(job_dir)
            with mock.patch.object(xta.x_photo_card, "build_thread_photo", return_value=None):
                payload = xta.build_x_thread(job_dir, humanize=False)
            self.assertIn("pubmed.ncbi.nlm.nih.gov/999", payload["sources_text"])
            for tweet in payload["tweets"]:
                self.assertNotIn("pubmed", tweet["text"])


class ThreadPhotoTests(unittest.TestCase):
    def _write_job(self, job_dir):
        (job_dir / "video_meta.json").write_text(json.dumps({
            "topic": "치매 예방", "main_keyword": "치매 예방", "hook_type": "반전형",
            "title": "치매 예방 습관", "core_message": "작은 습관부터 시작하세요",
            "hashtags": "#치매예방 #뇌건강",
        }, ensure_ascii=False), encoding="utf-8")
        (job_dir / "scenes.json").write_text(json.dumps([
            {"text": "훅 문장입니다.", "visual_query": "a"},
            {"text": "CTA 문장입니다.", "visual_query": "d"},
        ], ensure_ascii=False), encoding="utf-8")
        (job_dir / "strategy.json").write_text(
            json.dumps({"cta_next": "수면과 기억력"}, ensure_ascii=False), encoding="utf-8",
        )

    def test_payload_records_the_rendered_photo_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._write_job(job_dir)
            cp.build_content_package(job_dir)
            with mock.patch.object(
                xta.x_photo_card, "build_thread_photo", return_value=job_dir / "x_thread_photo.png",
            ):
                payload = xta.build_x_thread(job_dir, humanize=False)
            self.assertTrue(payload["photo_path"].endswith("x_thread_photo.png"))

    def test_photo_path_is_none_when_rendering_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._write_job(job_dir)
            cp.build_content_package(job_dir)
            with mock.patch.object(xta.x_photo_card, "build_thread_photo", return_value=None):
                payload = xta.build_x_thread(job_dir, humanize=False)
            self.assertIsNone(payload["photo_path"])

    def test_missing_pubmed_status_file_does_not_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            self._write_job(job_dir)
            cp.build_content_package(job_dir)
            with mock.patch.object(xta.x_photo_card, "build_thread_photo", return_value=None):
                payload = xta.build_x_thread(job_dir, humanize=False)
            self.assertIsNotNone(payload)
            self.assertEqual(payload["sources_text"], "")


if __name__ == "__main__":
    unittest.main()
