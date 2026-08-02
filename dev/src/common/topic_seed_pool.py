"""topic_seed_pool.py — the seed pool trend_probe needs but no human supplies.

Priority order, deduplicated by normalized seed text:

    1. research_categories.json category keywords (the channel's research axes)
    2. the `keywords` table in youtube_feedback.db (published title/tag history)
    3. topic_pipeline.json's extra_seeds (operator-pinned)

Every seed is normalized (lower/strip, length-bounded) and run through
trend_probe's existing commercial-language filter before it gets anywhere
near a suggest API call — the same filter `probe()` applies to results, just
applied to the seed itself first.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import trend_probe

RESEARCH_CATEGORIES_PATH = Path(__file__).resolve().parent / "research_categories.json"


@dataclass(frozen=True)
class SeedItem:
    seed: str
    origin: str  # research_categories | keywords_db | extra
    category_id: str | None = None
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "seed": self.seed, "origin": self.origin,
            "category_id": self.category_id, "weight": self.weight,
        }


def _normalize(term: Any, min_len: int, max_len: int) -> str | None:
    text = " ".join(str(term).split()).strip().lower()
    if not (min_len <= len(text) <= max_len):
        return None
    if trend_probe.COMMERCIAL.search(text):
        return None
    return text


def _from_research_categories(
    categories_path: str | Path, max_per_category: int, min_len: int, max_len: int,
) -> list[SeedItem]:
    try:
        raw = json.loads(Path(categories_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    items: list[SeedItem] = []
    for entry in raw.get("categories") or ():
        category_id = entry.get("category_id")
        count = 0
        for keyword in entry.get("keywords") or ():
            if count >= max_per_category:
                break
            normalized = _normalize(keyword, min_len, max_len)
            if normalized is None:
                continue
            items.append(SeedItem(seed=normalized, origin="research_categories", category_id=category_id))
            count += 1
    return items


def _from_keywords_db(conn, min_len: int, max_len: int) -> list[SeedItem]:
    if conn is None:
        return []
    try:
        rows = list(conn.execute("SELECT DISTINCT keyword FROM keywords"))
    except Exception:
        return []
    items: list[SeedItem] = []
    for (keyword,) in rows:
        normalized = _normalize(keyword, min_len, max_len)
        if normalized is not None:
            items.append(SeedItem(seed=normalized, origin="keywords_db"))
    return items


def _from_extra(extra_seeds: Iterable[str], min_len: int, max_len: int) -> list[SeedItem]:
    items: list[SeedItem] = []
    for seed in extra_seeds or ():
        normalized = _normalize(seed, min_len, max_len)
        if normalized is not None:
            items.append(SeedItem(seed=normalized, origin="extra"))
    return items


def build_seed_pool(
    conn=None,
    categories_path: str | Path | None = None,
    extra_seeds: Iterable[str] = (),
    *,
    max_seeds_total: int = 60,
    max_seeds_per_category: int = 8,
    min_seed_len: int = 2,
    max_seed_len: int = 20,
) -> list[SeedItem]:
    """Build the seed pool with no seed argument required from a human.

    `conn` is an optional sqlite3 connection to youtube_feedback.db (any
    connection exposing `.execute()`, so tests can pass an in-memory one). A
    missing/broken connection just shrinks the pool rather than raising —
    same failure posture as trend_probe.build_channel_vocabulary.
    """
    categories_path = categories_path or RESEARCH_CATEGORIES_PATH
    ordered = (
        _from_research_categories(categories_path, max_seeds_per_category, min_seed_len, max_seed_len)
        + _from_keywords_db(conn, min_seed_len, max_seed_len)
        + _from_extra(extra_seeds, min_seed_len, max_seed_len)
    )
    seen: set[str] = set()
    pool: list[SeedItem] = []
    for item in ordered:
        if item.seed in seen:
            continue
        seen.add(item.seed)
        pool.append(item)
        if len(pool) >= max_seeds_total:
            break
    return pool
