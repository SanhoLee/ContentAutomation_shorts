"""topic_seed_pool.py — the seed pool trend_probe needs but no human supplies.

Priority order, deduplicated by normalized seed text:

    1. research_categories.json category keywords (the channel's research axes)
    2. the `keywords` table in youtube_feedback.db (published title/tag history)
    3. topic_pipeline.json's extra_seeds (operator-pinned)

Every seed is normalized (lower/strip, length-bounded) and run through
trend_probe's existing commercial-language filter before it gets anywhere
near a suggest API call — the same filter `probe()` applies to results, just
applied to the seed itself first.

Risk-factor seeds are then *anchored* (see topic_domain): `고혈압` becomes
`치매 고혈압`, because the category it comes from is only in the channel's scope
via `AND cognitive decline` — a half of the PubMed query that never used to
reach the seed, which is how straight cardiology ended up in the queue.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import topic_domain
import trend_probe

RESEARCH_CATEGORIES_PATH = Path(__file__).resolve().parent / "research_categories.json"


@dataclass(frozen=True)
class SeedItem:
    seed: str
    origin: str  # research_categories | keywords_db | extra
    category_id: str | None = None
    weight: float = 1.0
    # Set only on anchored seeds: `seed` is what gets probed, `base_seed` is what
    # it was before the anchor went on, so the probe can fall back to it.
    base_seed: str | None = None
    anchor: str | None = None

    @property
    def anchored(self) -> bool:
        return self.anchor is not None


def _normalize(term: Any, min_len: int, max_len: int) -> str | None:
    text = " ".join(str(term).split()).strip().lower()
    if not (min_len <= len(text) <= max_len):
        return None
    if trend_probe.COMMERCIAL.search(text):
        return None
    return text


def _anchored_items(
    normalized: str, origin: str, category_id: str | None, domain: topic_domain.Domain,
) -> list[SeedItem]:
    """One SeedItem per probe-worthy variant of `normalized`.

    The length bounds are deliberately *not* re-applied here: they exist to keep
    junk keywords out, and `고혈압` passing them means `치매 고혈압` is fine too.
    Re-checking would silently drop exactly the seeds anchoring is for.
    """
    return [
        SeedItem(
            seed=variant, origin=origin, category_id=category_id,
            base_seed=normalized if anchor else None, anchor=anchor,
        )
        for variant, anchor in topic_domain.anchor_variants(normalized, category_id, domain)
    ]


def _from_research_categories(
    categories_path: str | Path, max_per_category: int, min_len: int, max_len: int,
    domain: topic_domain.Domain,
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
            # A keyword counts once against the per-category cap no matter how
            # many anchored variants it expands into.
            items.extend(_anchored_items(normalized, "research_categories", category_id, domain))
            count += 1
    return items


def _from_keywords_db(conn, min_len: int, max_len: int, domain: topic_domain.Domain) -> list[SeedItem]:
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
            # No category_id, so these follow the domain's default_role.
            items.extend(_anchored_items(normalized, "keywords_db", None, domain))
    return items


def _from_extra(extra_seeds: Iterable[str], min_len: int, max_len: int, domain: topic_domain.Domain) -> list[SeedItem]:
    items: list[SeedItem] = []
    for seed in extra_seeds or ():
        normalized = _normalize(seed, min_len, max_len)
        if normalized is not None:
            items.extend(_anchored_items(normalized, "extra", None, domain))
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
    domain: topic_domain.Domain | None = None,
) -> list[SeedItem]:
    """Build the seed pool with no seed argument required from a human.

    `conn` is an optional sqlite3 connection to youtube_feedback.db (any
    connection exposing `.execute()`, so tests can pass an in-memory one). A
    missing/broken connection just shrinks the pool rather than raising —
    same failure posture as trend_probe.build_channel_vocabulary.

    Dedup and `max_seeds_total` are applied to the *anchored* seed text, so
    anchoring cannot push the suggest-API call count past the configured cap.
    """
    categories_path = categories_path or RESEARCH_CATEGORIES_PATH
    domain = domain or topic_domain.load_domain()
    ordered = (
        _from_research_categories(categories_path, max_seeds_per_category, min_seed_len, max_seed_len, domain)
        + _from_keywords_db(conn, min_seed_len, max_seed_len, domain)
        + _from_extra(extra_seeds, min_seed_len, max_seed_len, domain)
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
