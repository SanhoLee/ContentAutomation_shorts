import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

import trend_probe as tp

# Stand-in for the channel vocabulary that build_channel_vocabulary derives from
# the keywords table and research_categories.json.
VOCAB = ("치매", "기억", "뇌", "수면", "운동", "예방", "dementia", "memory", "brain")


def stub(by_locale):
    """Return canned suggestions per hl value and record which locales ran."""
    asked = []

    def fetch(url, params):
        asked.append(params["hl"])
        assert params.get("oe") == "utf-8", "oe=utf-8 없이 요청하면 한국어가 깨진다"
        return ["seed", list(by_locale.get(params["hl"], []))]

    fetch.asked = asked
    return fetch


class DriftGateTests(unittest.TestCase):
    def test_ambiguous_seed_is_rejected_in_every_language(self):
        # The pollution already in trend_observations: seed "꿈" returned an
        # anime, a mascot and a dream-interpretation site.
        noise = ["꿈빛파티시엘", "꿈돌이", "꿈트렁", "꿈해몽", "기차의 꿈"]
        result = tp.probe("꿈", VOCAB, fetch=stub({"en": noise, "ja": noise, "ko": noise}),
                          include_youtube=False)
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "off_topic")
        self.assertEqual(fetch_locales(result), ["en", "ja", "ko"])

    def test_on_topic_seed_passes(self):
        good = ["치매 예방", "치매 예방 게임", "치매 예방법", "치매 예방 두뇌 운동"]
        result = tp.probe("치매 예방", VOCAB, fetch=stub({"en": good}), include_youtube=False)
        self.assertTrue(result.ok)
        self.assertEqual(result.domain_match, 1.0)

    def test_domain_match_ratio_counts_vocabulary_hits(self):
        self.assertEqual(tp.domain_match_ratio(["치매 예방", "꿈돌이"], VOCAB), 0.5)

    def test_empty_vocabulary_disables_the_gate(self):
        # A fresh channel with no published keywords must not reject everything.
        self.assertEqual(tp.domain_match_ratio(["아무거나"], ()), 1.0)


class LanguageLadderTests(unittest.TestCase):
    def test_stops_at_the_first_passing_language(self):
        fetch = stub({"en": ["dementia prevention", "brain memory test", "sleep and memory"]})
        result = tp.probe("dementia", VOCAB, fetch=fetch, include_youtube=False)
        self.assertTrue(result.ok)
        self.assertEqual(fetch.asked, ["en"], "첫 언어가 통과하면 ja/ko는 조회하지 않는다")

    def test_falls_through_to_a_later_language(self):
        fetch = stub({
            "en": ["spice blend recipe", "unrelated thing", "another one"],
            "ja": ["認知症 予防", "脳 記憶", "睡眠"],
            "ko": ["치매 예방", "기억력", "뇌 건강"],
        })
        result = tp.probe("認知症", ("認知症", "脳", "記憶", "睡眠"), fetch=fetch, include_youtube=False)
        self.assertTrue(result.ok)
        self.assertEqual(result.language, "ja")
        self.assertEqual(fetch.asked, ["en", "ja"])

    def test_language_failure_advances_instead_of_raising(self):
        def flaky(url, params):
            if params["hl"] == "en":
                raise OSError("blocked")
            return ["seed", ["치매 예방", "기억력 감퇴", "뇌 건강"]]
        result = tp.probe("치매", VOCAB, fetch=flaky, include_youtube=False)
        self.assertTrue(result.ok)
        self.assertEqual(result.language, "ja")


class CommercialFilterTests(unittest.TestCase):
    def test_commercial_suggestions_are_dropped(self):
        mixed = ["치매 예방", "치매 예방 영양제 추천", "기억력 좋아지는 법",
                 "brain supplement price", "뇌 건강"]
        result = tp.probe("치매", VOCAB, fetch=stub({"en": mixed}), include_youtube=False)
        self.assertTrue(result.ok)
        self.assertEqual(result.dropped_commercial, 2)
        self.assertNotIn("치매 예방 영양제 추천", result.keywords)

    def test_filter_covers_all_three_languages(self):
        for spam in ("영양제 추천", "supplement price", "サプリ おすすめ"):
            self.assertTrue(tp.COMMERCIAL.search(spam), spam)


class VocabularyTests(unittest.TestCase):
    def test_categories_file_alone_yields_a_usable_vocabulary(self):
        vocabulary = tp.build_channel_vocabulary(None)
        self.assertIn("수면", vocabulary)
        self.assertIn("청력", vocabulary)

    def test_broken_connection_degrades_instead_of_raising(self):
        class Broken:
            def execute(self, *args):
                raise RuntimeError("no such table")
        self.assertTrue(tp.build_channel_vocabulary(Broken()))


def fetch_locales(result):
    return [attempt["language"] for attempt in result.attempts]


if __name__ == "__main__":
    unittest.main()
