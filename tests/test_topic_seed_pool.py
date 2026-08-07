import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

import topic_domain
import topic_seed_pool as tsp

CATEGORIES_FIXTURE = {
    "categories": [
        {
            "category_id": "memory_cognition",
            "keywords": ["기억", "건망증", "인지", "치매", "뇌", "집중력", "판단력", "기억력", "cognition"],
        },
        {
            "category_id": "sleep_cognition",
            "keywords": ["수면", "불면", "잠"],
        },
        {
            "category_id": "chronic_disease",
            "keywords": ["고혈압", "심혈관", "혈당"],
        },
    ]
}

DOMAIN_SPEC = {
    "domain_id": "brain_health",
    "label_ko": "뇌 건강",
    "require_anchor": True,
    "anchor_terms": ["뇌", "치매", "기억", "기억력", "인지", "건망증", "집중력"],
    "seed_anchors": ["치매", "뇌"],
    "max_anchor_variants": 1,
    "category_roles": {
        "chronic_disease": "risk_factor",
        "memory_cognition": "core",
        "sleep_cognition": "core",
    },
    "default_role": "core",
}


def write_categories(tmp_path):
    path = Path(tmp_path) / "categories.json"
    path.write_text(json.dumps(CATEGORIES_FIXTURE, ensure_ascii=False), encoding="utf-8")
    return path


def make_domain(tmp_path, overrides=None):
    path = Path(tmp_path) / "topic_domain.json"
    path.write_text(json.dumps({**DOMAIN_SPEC, **(overrides or {})}, ensure_ascii=False), encoding="utf-8")
    return topic_domain.load_domain(path)


class SeedPoolFromCategoriesTests(unittest.TestCase):
    def test_pulls_keywords_from_every_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            categories_path = write_categories(tmp)
            pool = tsp.build_seed_pool(None, categories_path, max_seeds_total=100, max_seeds_per_category=8)
            origins = {item.origin for item in pool}
            self.assertEqual(origins, {"research_categories"})
            seeds = {item.seed for item in pool}
            self.assertIn("치매", seeds)
            self.assertIn("수면", seeds)

    def test_caps_seeds_per_category(self):
        with tempfile.TemporaryDirectory() as tmp:
            categories_path = write_categories(tmp)
            pool = tsp.build_seed_pool(None, categories_path, max_seeds_total=100, max_seeds_per_category=2)
            memory_items = [item for item in pool if item.category_id == "memory_cognition"]
            self.assertLessEqual(len(memory_items), 2)

    def test_caps_total_seed_pool_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            categories_path = write_categories(tmp)
            pool = tsp.build_seed_pool(None, categories_path, max_seeds_total=3, max_seeds_per_category=8)
            self.assertLessEqual(len(pool), 3)

    def test_missing_categories_file_degrades_to_empty(self):
        pool = tsp.build_seed_pool(None, "/no/such/categories.json", max_seeds_total=10)
        self.assertEqual(pool, [])


class SeedPoolFromKeywordsDbTests(unittest.TestCase):
    def test_distinct_published_keywords_become_seeds(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE keywords (video_id TEXT, keyword TEXT, source TEXT)")
        conn.executemany(
            "INSERT INTO keywords VALUES (?, ?, ?)",
            [("v1", "치매 예방", "title"), ("v2", "치매 예방", "title"), ("v3", "수면 습관", "tag")],
        )
        pool = tsp.build_seed_pool(conn, "/no/such/categories.json", max_seeds_total=10)
        seeds = [item.seed for item in pool]
        self.assertEqual(seeds.count("치매 예방"), 1, "중복 키워드는 한 번만 시드가 되어야 한다")
        self.assertIn("수면 습관", seeds)
        self.assertTrue(all(item.origin == "keywords_db" for item in pool))

    def test_broken_connection_degrades_instead_of_raising(self):
        class Broken:
            def execute(self, *args):
                raise RuntimeError("no such table")

        pool = tsp.build_seed_pool(Broken(), "/no/such/categories.json", max_seeds_total=10)
        self.assertEqual(pool, [])


class SeedPoolExtraAndNormalizationTests(unittest.TestCase):
    def test_extra_seeds_are_included(self):
        pool = tsp.build_seed_pool(None, "/no/such/categories.json", extra_seeds=["새벽 각성"], max_seeds_total=10)
        self.assertEqual([item.seed for item in pool], ["새벽 각성"])
        self.assertEqual(pool[0].origin, "extra")

    def test_seeds_outside_length_bounds_are_dropped(self):
        pool = tsp.build_seed_pool(
            None, "/no/such/categories.json", extra_seeds=["a", "매우매우매우매우매우매우매우긴시드문자열입니다요"],
            max_seeds_total=10, min_seed_len=2, max_seed_len=20,
        )
        self.assertEqual(pool, [])

    def test_commercial_seeds_are_filtered_out(self):
        pool = tsp.build_seed_pool(None, "/no/such/categories.json", extra_seeds=["영양제 추천"], max_seeds_total=10)
        self.assertEqual(pool, [])

    def test_priority_order_dedupes_by_origin_priority(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE keywords (video_id TEXT, keyword TEXT, source TEXT)")
        conn.execute("INSERT INTO keywords VALUES ('v1', '치매', 'title')")
        with tempfile.TemporaryDirectory() as tmp:
            categories_path = write_categories(tmp)
            pool = tsp.build_seed_pool(conn, categories_path, extra_seeds=["치매"], max_seeds_total=100)
            matches = [item for item in pool if item.seed == "치매"]
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].origin, "research_categories")


class SeedAnchoringTests(unittest.TestCase):
    """Risk-factor seeds carry the `AND cognitive decline` half of their
    category's PubMed query that used to be dropped on the way to the probe."""

    def _pool(self, tmp, **kwargs):
        return tsp.build_seed_pool(
            None, write_categories(tmp), domain=make_domain(tmp, kwargs.pop("domain_overrides", None)),
            max_seeds_total=100, max_seeds_per_category=8, **kwargs,
        )

    def test_risk_factor_seeds_are_anchored(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = self._pool(tmp)
            chronic = {item.seed: item for item in pool if item.category_id == "chronic_disease"}
            self.assertIn("치매 고혈압", chronic)
            self.assertIn("치매 심혈관", chronic)
            self.assertNotIn("고혈압", chronic)
            self.assertEqual(chronic["치매 고혈압"].anchor, "치매")
            self.assertEqual(chronic["치매 고혈압"].base_seed, "고혈압")
            self.assertTrue(chronic["치매 고혈압"].anchored)

    def test_core_seeds_are_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = self._pool(tmp)
            core = {item.seed: item for item in pool if item.category_id == "sleep_cognition"}
            self.assertIn("수면", core)
            self.assertIsNone(core["수면"].anchor)
            self.assertIsNone(core["수면"].base_seed)
            self.assertFalse(core["수면"].anchored)

    def test_anchoring_is_not_re_length_checked(self):
        """`max_seed_len` gates raw keywords; an anchor pushing past it must not
        silently delete exactly the seeds anchoring exists for."""
        with tempfile.TemporaryDirectory() as tmp:
            pool = tsp.build_seed_pool(
                None, write_categories(tmp), domain=make_domain(tmp),
                max_seeds_total=100, max_seeds_per_category=8, max_seed_len=4,
            )
            seeds = {item.seed for item in pool}
            self.assertIn("치매 고혈압", seeds)  # 6 chars, over max_seed_len

    def test_max_anchor_variants_bounds_the_probe_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            one = self._pool(tmp)
            two = self._pool(tmp, domain_overrides={"max_anchor_variants": 2})
            chronic_one = [i for i in one if i.category_id == "chronic_disease"]
            chronic_two = [i for i in two if i.category_id == "chronic_disease"]
            self.assertEqual(len(chronic_one), 3)
            self.assertEqual(len(chronic_two), 6)

    def test_total_cap_is_applied_to_anchored_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = tsp.build_seed_pool(
                None, write_categories(tmp), domain=make_domain(tmp, {"max_anchor_variants": 2}),
                max_seeds_total=5,
            )
            self.assertEqual(len(pool), 5)

    def test_keywords_db_seeds_follow_the_default_role(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE keywords (video_id TEXT, keyword TEXT, source TEXT)")
        conn.execute("INSERT INTO keywords VALUES ('v1', '콜레스테롤', 'title')")
        with tempfile.TemporaryDirectory() as tmp:
            bare = tsp.build_seed_pool(conn, "/no/such/categories.json", domain=make_domain(tmp), max_seeds_total=10)
            self.assertEqual([i.seed for i in bare], ["콜레스테롤"])

            anchored = tsp.build_seed_pool(
                conn, "/no/such/categories.json",
                domain=make_domain(tmp, {"default_role": "risk_factor"}), max_seeds_total=10,
            )
            self.assertEqual([i.seed for i in anchored], ["치매 콜레스테롤"])

    def test_seed_item_to_dict_carries_the_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            pool = self._pool(tmp)
            item = next(i for i in pool if i.seed == "치매 고혈압")
            self.assertEqual(item.to_dict()["anchor"], "치매")
            self.assertEqual(item.to_dict()["base_seed"], "고혈압")


if __name__ == "__main__":
    unittest.main()
