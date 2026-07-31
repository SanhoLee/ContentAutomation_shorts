"""Category-level PubMed research depth signals for topic planning.

This module turns per-category PubMed literature volume/recency data into a
normalized 0..1 "research_depth" signal that `objective_planner.build_candidate_pool`
can blend into candidate scoring, plus a cooldown gate that prevents
low-volume-but-high-recency categories (e.g. hearing/vision, social isolation)
from being over-used before enough new literature accumulates.

Data source: manually refreshed via dev/sh (or ops) using NCBI E-utilities.
This module deliberately does NOT call the network at runtime — pipeline
stages must stay deterministic and budget-bounded. Volumes are refreshed
periodically (see docs/usage) and stored in research_categories.json.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CATEGORIES_PATH = SCRIPT_DIR / "research_categories.json"

# Weight given to recency vs. raw volume when computing potential_score.
# Recency is weighted higher because volume only answers "will we run out of
# material", while recency answers "is this a fresh hook right now" — see
# PROJECT_CONTEXT discussion 2026-07-26.
VOLUME_WEIGHT = 0.4
RECENCY_WEIGHT = 0.6

# Cooldown tiers by absolute paper volume. Categories with thin literature
# (e.g. hearing/vision, social isolation) get long cooldowns so they are not
# exhausted even though their potential_score is high.
COOLDOWN_TIERS = (
    (8000, 0),
    (5000, 3),
    (1000, 7),
    (0, 14),
)


@dataclass(frozen=True)
class CategorySignal:
    category_id: str
    label_ko: str
    keywords: tuple[str, ...]
    # The vetted English PubMed query the volumes were measured with. Also the
    # last rung of evidence_probe's ladder, which is why it must survive the
    # load: it is what a topic falls back to instead of a Korean query.
    query: str
    total_count: int
    recent_5y_count: int
    recent_ratio_pct: float
    volume_score: float
    potential_score: float
    cooldown_days: int


def _cooldown_for_volume(total_count: int) -> int:
    for floor, days in COOLDOWN_TIERS:
        if total_count >= floor:
            return days
    return COOLDOWN_TIERS[-1][1]


def load_category_signals(path: str | Path | None = None) -> dict[str, CategorySignal]:
    """Load category research signals from research_categories.json.

    Returns an empty dict (never raises) if the file is missing so that
    candidate scoring degrades gracefully to a neutral 0.5 signal rather
    than failing the whole planning run.
    """
    target = Path(path) if path else DEFAULT_CATEGORIES_PATH
    if not target.is_file():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    entries = raw.get("categories") if isinstance(raw, Mapping) else None
    if not isinstance(entries, list):
        return {}

    counts = [
        int(entry.get("total_count") or 0)
        for entry in entries
        if isinstance(entry, Mapping)
    ]
    max_count = max(counts) if counts else 0

    signals: dict[str, CategorySignal] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        category_id = str(entry.get("category_id") or "").strip()
        if not category_id:
            continue
        total_count = int(entry.get("total_count") or 0)
        recent_count = int(entry.get("recent_5y_count") or 0)
        recent_ratio = float(entry.get("recent_ratio_pct") or 0.0)
        volume_score = (total_count / max_count * 100.0) if max_count else 0.0
        potential_score = VOLUME_WEIGHT * volume_score + RECENCY_WEIGHT * recent_ratio
        signals[category_id] = CategorySignal(
            category_id=category_id,
            label_ko=str(entry.get("label_ko") or category_id),
            keywords=tuple(str(k) for k in (entry.get("keywords") or ())),
            query=str(entry.get("query") or ""),
            total_count=total_count,
            recent_5y_count=recent_count,
            recent_ratio_pct=recent_ratio,
            volume_score=round(volume_score, 2),
            potential_score=round(potential_score, 2),
            cooldown_days=int(entry.get("cooldown_days_override") or _cooldown_for_volume(total_count)),
        )
    return signals


def match_category(topic_family: str, signals: Mapping[str, CategorySignal]) -> CategorySignal | None:
    """Map a topic_family (from objective_planner._topic_family) or free text
    to the best-matching research category via keyword overlap."""
    text = (topic_family or "").strip().lower()
    if not text:
        return None
    for signal in signals.values():
        if any(keyword.lower() in text for keyword in signal.keywords):
            return signal
    return signals.get(text)


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def research_depth_metric(
    topic_family: str,
    signals: Mapping[str, CategorySignal],
    usage_log: Mapping[str, str] | None = None,
    *,
    today: date | None = None,
) -> tuple[float, dict[str, Any]]:
    """Return (normalized_0_1_signal, debug_info) for use as
    candidate['normalized_metrics']['research_depth'].

    If the category is under cooldown, the signal is floored to 0.05 so the
    candidate is still scoreable (never hard-excluded here — planner-level
    filtering happens in objective_planner) but is effectively deprioritized.
    Unmatched topics fall back to a neutral 0.5, matching the convention
    already used elsewhere in objective_planner (e.g. metrics.get(name, 0.5)).
    """
    signal = match_category(topic_family, signals)
    if signal is None:
        return 0.5, {"matched": False, "reason": "no_category_match"}

    today = today or date.today()
    last_used = _parse_date((usage_log or {}).get(signal.category_id))
    in_cooldown = False
    days_since_use = None
    if last_used is not None:
        days_since_use = (today - last_used).days
        in_cooldown = days_since_use < signal.cooldown_days

    if in_cooldown:
        return 0.05, {
            "matched": True,
            "category_id": signal.category_id,
            "in_cooldown": True,
            "days_since_use": days_since_use,
            "cooldown_days": signal.cooldown_days,
            "potential_score": signal.potential_score,
        }

    # Reward categories that just cleared cooldown a little more than ones
    # scored far in advance of ever being used, capped at +20%.
    overdue_bonus = 1.0
    if last_used is not None and signal.cooldown_days > 0:
        overdue_ratio = min(days_since_use / signal.cooldown_days, 2.0)
        overdue_bonus = 0.8 + 0.2 * overdue_ratio

    normalized = min(1.0, (signal.potential_score / 100.0) * overdue_bonus)
    return normalized, {
        "matched": True,
        "category_id": signal.category_id,
        "in_cooldown": False,
        "days_since_use": days_since_use,
        "cooldown_days": signal.cooldown_days,
        "potential_score": signal.potential_score,
        "overdue_bonus": round(overdue_bonus, 4),
    }


def load_usage_log(path: str | Path | None = None) -> dict[str, str]:
    """category_id -> last used YYYY-MM-DD. Missing file returns {}."""
    target = Path(path) if path else (SCRIPT_DIR / "research_category_usage.json")
    if not target.is_file():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, Mapping) else {}


def record_category_usage(
    category_id: str,
    path: str | Path | None = None,
    *,
    today: date | None = None,
) -> None:
    target = Path(path) if path else (SCRIPT_DIR / "research_category_usage.json")
    usage = load_usage_log(target)
    usage[category_id] = (today or date.today()).isoformat()
    target.write_text(json.dumps(usage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
