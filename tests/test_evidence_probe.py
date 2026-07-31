import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

import evidence_probe as ep


def stub(responses):
    """Answer each query from a dict, recording what was actually sent.

    No live network: the ladder's job is deciding what to ask and what to
    believe, and both are decidable from canned counts.
    """
    sent = []

    def fetch(url, params):
        query = params.get("query") or params.get("term") or ""
        sent.append(query)
        value = responses.get(query, 0)
        if url == ep.PUBMED_ESEARCH:
            count, translation = value if isinstance(value, tuple) else (value, query)
            ids = [str(90000 + i) for i in range(min(3, count))]
            return {"esearchresult": {"count": str(count), "idlist": ids,
                                      "querytranslation": translation}}
        return {"hitCount": value,
                "resultList": {"result": [{"pubYear": "2025", "citedByCount": 7}] if value else []}}

    fetch.sent = sent
    return fetch


class LadderTests(unittest.TestCase):
    def test_narrowing_finds_what_the_full_query_missed(self):
        # The shipped failure: 0 hits for the full query, 30 once two modifiers
        # ("uncorrected", "risk") come off.
        fetch = stub({"uncorrected refractive error dementia risk": 0,
                      "refractive error dementia": 30})
        result = ep.probe_pubmed("안경/치매", "uncorrected refractive error dementia risk", fetch=fetch)
        self.assertTrue(result.ok)
        self.assertEqual(result.ladder_rung, "narrowed")
        self.assertEqual(result.resolved_query, "refractive error dementia")

    def test_outcome_terms_survive_narrowing(self):
        self.assertIn("dementia", ep.narrow_query("uncorrected refractive error dementia risk"))
        self.assertNotIn("uncorrected", ep.narrow_query("uncorrected refractive error dementia risk"))

    def test_ladder_stops_at_the_first_good_rung(self):
        fetch = stub({"visual impairment dementia": 4779})
        result = ep.probe("주제", "visual impairment dementia", fetch=fetch)
        self.assertEqual(result.ladder_rung, "full")
        self.assertEqual(len(fetch.sent), 1, "첫 rung이 통과하면 더 조회하지 않아야 한다")


class EnglishOnlyTests(unittest.TestCase):
    def test_korean_is_never_sent(self):
        # The poisoning case: a Claude budget stop left the raw Korean topic as
        # the query, PubMed kept only "LDL", and unrelated papers became evidence.
        fetch = stub({"LDL": (134890, '"ldl"[All Fields]'),
                      "(diabetes OR hypertension) AND cognitive decline": 11421})
        result = ep.probe_pubmed(
            "중년 LDL", "중년 고LDL 콜레스테롤, 뇌혈관 건강, 치매 위험",
            category_query="(diabetes OR hypertension) AND cognitive decline", fetch=fetch)
        self.assertTrue(result.ok)
        self.assertEqual(result.ladder_rung, "category")
        for query in fetch.sent:
            self.assertFalse(ep.has_hangul(query), f"한글 쿼리가 전송됨: {query!r}")

    def test_ladder_is_empty_when_nothing_english_is_available(self):
        result = ep.probe("주제", "한글만 있는 검색어", fetch=stub({}))
        self.assertEqual(result.status, ep.STATUS_NO_ENGLISH_QUERY)

    def test_build_ladder_drops_hangul_and_duplicates(self):
        rungs = ep.build_ladder("치매 dementia", "dementia")
        self.assertTrue(all(not ep.has_hangul(q) for _, q in rungs))
        self.assertEqual(len(rungs), len({q for _, q in rungs}))


class GuardTests(unittest.TestCase):
    def test_absurdly_broad_result_is_rejected(self):
        fetch = stub({"LDL": (134890, '"ldl"[All Fields]'), "fallback query": 900})
        result = ep.probe_pubmed("주제", "LDL", category_query="fallback query", fetch=fetch)
        self.assertEqual(result.ladder_rung, "category")
        rejected = [a for a in result.attempts if a.get("reason", "").startswith("too_broad")]
        self.assertTrue(rejected, "13만 건짜리 매칭은 거부되어야 한다")

    def test_mangled_query_is_rejected_even_with_hits(self):
        # PubMed kept none of our terms: the hits answer a different question.
        fetch = stub({"tinnitus cognition decline": (500, '"unrelated"[All Fields]')})
        result = ep.probe_pubmed("주제", "tinnitus cognition decline", fetch=fetch)
        self.assertFalse(result.ok)
        self.assertIn("query_mangled", result.attempts[0]["reason"])

    def test_query_survival_measures_kept_terms(self):
        self.assertEqual(ep.query_survival("hearing loss dementia", "hearing loss dementia[MeSH]"), 1.0)
        self.assertLess(ep.query_survival("hearing loss dementia", '"ldl"[All Fields]'), 0.5)

    def test_too_few_hits_advances_the_ladder(self):
        fetch = stub({"rare thing dementia": 2, "rare dementia": 900})
        result = ep.probe("주제", "rare thing dementia", category_query="rare dementia", fetch=fetch)
        self.assertTrue(result.ok)
        self.assertNotEqual(result.ladder_rung, "full")

    def test_exhausted_ladder_reports_no_results(self):
        result = ep.probe("주제", "nothing here dementia", fetch=stub({}))
        self.assertEqual(result.status, ep.STATUS_NO_RESULTS)
        self.assertEqual(result.ladder_rung, "exhausted")

    def test_network_failure_does_not_raise(self):
        def broken(url, params):
            raise OSError("network down")
        result = ep.probe("주제", "dementia risk", fetch=broken)
        self.assertEqual(result.status, ep.STATUS_NO_RESULTS)


class MetricTests(unittest.TestCase):
    def test_zero_evidence_scores_zero(self):
        failed = ep.EvidenceResult(topic="t", resolved_query="q", ladder_rung="exhausted",
                                   status=ep.STATUS_NO_RESULTS)
        self.assertEqual(set(ep.evidence_metrics(failed).values()), {0.0})

    def test_metrics_stay_within_unit_range(self):
        result = ep.EvidenceResult(topic="t", resolved_query="q", ladder_rung="full",
                                   status=ep.STATUS_OK, hit_count=999_999,
                                   recent_count=999_999, median_citations=90_000)
        for name, value in ep.evidence_metrics(result).items():
            self.assertGreaterEqual(value, 0.0, name)
            self.assertLessEqual(value, 1.0, name)

    def test_more_papers_scores_higher(self):
        def volume(hits):
            return ep.evidence_metrics(ep.EvidenceResult(
                topic="t", resolved_query="q", ladder_rung="full",
                status=ep.STATUS_OK, hit_count=hits))["evidence_volume"]
        self.assertLess(volume(30), volume(4779))


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE evidence_observations (topic TEXT, query TEXT, resolved_query TEXT,"
            " ladder_rung TEXT, source TEXT, status TEXT, hit_count INTEGER, recent_count INTEGER,"
            " median_citations REAL, observed_date TEXT, PRIMARY KEY(topic, source, observed_date))"
        )

    def test_recorded_observation_is_read_back_as_cache(self):
        result = ep.EvidenceResult(topic="치매 예방", resolved_query="dementia prevention",
                                   ladder_rung="full", status=ep.STATUS_OK, hit_count=4779)
        ep.record_observation(self.conn, result)
        cached = ep.cached_observation(self.conn, "치매 예방")
        self.assertIsNotNone(cached)
        self.assertEqual(cached.hit_count, 4779)

    def test_missing_topic_is_a_cache_miss(self):
        self.assertIsNone(ep.cached_observation(self.conn, "본 적 없는 주제"))

    def test_persistence_never_raises_without_a_table(self):
        bare = sqlite3.connect(":memory:")
        result = ep.EvidenceResult(topic="t", resolved_query="q", ladder_rung="full",
                                   status=ep.STATUS_OK)
        ep.record_observation(bare, result)          # must not raise
        self.assertIsNone(ep.cached_observation(bare, "t"))


if __name__ == "__main__":
    unittest.main()
