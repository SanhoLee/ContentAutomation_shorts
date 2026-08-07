"""topic_score.py — deterministic 0-100 scoring for auto-generated topic candidates.

Per project convention (see docs/design/objective-driven-content-planner.md),
Claude never makes the final numeric call here — this module is pure Python
and every function is a straight string/set computation, so a score is always
reproducible from its inputs alone.

Six weighted components (default weights sum to 100, configurable via
topic_pipeline's topic_score_rules.json):

    domain_relevance    — does the text sit inside the channel's base domain
    niche_relevance     — channel vocabulary hits in the candidate text
    search_intent_fit   — does the text look like a real search query
    evidence_potential  — does it point at researchable claims
    novelty_vs_history  — how different is it from recently published titles
    safety_tone         — absence of fear/cure-guarantee marketing language

`domain_relevance` is the newest and the heaviest, and it is backed by a hard
gate rather than points alone. `niche_relevance` scores the channel's whole
vocabulary, which includes risk factors like `심혈관` and `고혈압` — that is
correct for "does this channel talk about this word" and useless for "is this
about brains". Weighted scoring alone let `심혈관질환 증상` (60) beat
`뇌 건강 식단` (57), so anything with no anchor term is now rejected outright
(`missing_anchor`), regardless of total.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import topic_domain

DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "config" / "topic_score_rules.json"

DEFAULT_RULES: dict[str, Any] = {
    "threshold": 45,
    "eligible_top_k": 15,
    "weights": {
        "domain_relevance": 30,
        "niche_relevance": 15,
        "search_intent_fit": 15,
        "evidence_potential": 20,
        "novelty_vs_history": 15,
        "safety_tone": 5,
    },
    "intent_patterns": {
        "question": ["할까", "이란", "증상", "원인", "차이", "방법", "언제"],
        "compare": ["차이", "vs", "비교", "다른점"],
        "symptom": ["증상", "징후", "신호"],
    },
    "evidence_hints": ["연구", "논문", "예방", "인지", "신경", "수면", "운동", "식단", "치매", "기억"],
    "ban_keywords": ["완치", "보장", "기적", "100%", "즉시 회복"],
    "novelty": {"lookback_days": 60, "similarity_threshold": 0.75},
}


def load_rules(path: str | Path | None = None) -> dict[str, Any]:
    """Load scoring rules, degrading to the built-in defaults on any missing/invalid file."""
    target = Path(path) if path else DEFAULT_RULES_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    merged: dict[str, Any] = json.loads(json.dumps(DEFAULT_RULES))
    for key, value in raw.items():
        if key in ("weights", "novelty") and isinstance(value, dict):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _matched(text: str, terms: Iterable[str]) -> list[str]:
    lowered = text.lower()
    out: list[str] = []
    for term in terms:
        term = str(term)
        if term and term.lower() in lowered and term not in out:
            out.append(term)
    return out


def score_domain_relevance(
    text: str, domain: topic_domain.Domain, weight: float,
) -> tuple[float, list[str]]:
    """Graded, not binary: one anchor term is inside the domain, two is squarely
    inside it. Zero scores nothing and also trips `missing_anchor` below."""
    hits = topic_domain.matched_anchors(text, domain)
    if not hits:
        return 0.0, []
    return round(weight * min(1.0, (len(hits) + 1) / 3), 2), hits


def score_niche_relevance(text: str, vocabulary: Iterable[str], weight: float) -> tuple[float, list[str]]:
    hits = _matched(text, vocabulary)
    return round(weight * min(1.0, len(hits) / 3), 2), hits


def score_search_intent_fit(text: str, intent_patterns: Mapping[str, Sequence[str]], weight: float) -> float:
    categories_hit = sum(1 for patterns in intent_patterns.values() if _matched(text, patterns))
    return round(weight * min(1.0, categories_hit / 2), 2)


def score_evidence_potential(text: str, evidence_hints: Iterable[str], weight: float) -> float:
    hits = _matched(text, evidence_hints)
    return round(weight * min(1.0, len(hits) / 3), 2)


def score_novelty_vs_history(text: str, recent_titles: Sequence[str], weight: float) -> float:
    if not recent_titles:
        return float(weight)
    similarity = max(SequenceMatcher(None, text, title).ratio() for title in recent_titles)
    return round(weight * max(0.0, 1.0 - similarity), 2)


def score_safety_tone(text: str, ban_keywords: Iterable[str], weight: float) -> tuple[float, bool]:
    banned = bool(_matched(text, ban_keywords))
    return (0.0, True) if banned else (float(weight), False)


@dataclass(frozen=True)
class ScoreResult:
    total: int
    breakdown: dict[str, float]
    matched_terms: tuple[str, ...]
    banned: bool
    matched_anchors: tuple[str, ...] = ()
    missing_anchor: bool = False


def score_candidate(
    text: str,
    *,
    vocabulary: Iterable[str] = (),
    recent_titles: Sequence[str] = (),
    rules: dict[str, Any] | None = None,
    domain: topic_domain.Domain | None = None,
) -> ScoreResult:
    rules = rules or load_rules()
    domain = domain or topic_domain.load_domain()
    weights = rules.get("weights", DEFAULT_RULES["weights"])

    domain_score, anchors = score_domain_relevance(text, domain, weights.get("domain_relevance", 30))
    niche, matched = score_niche_relevance(text, vocabulary, weights.get("niche_relevance", 15))
    intent = score_search_intent_fit(text, rules.get("intent_patterns", {}), weights.get("search_intent_fit", 15))
    evidence = score_evidence_potential(text, rules.get("evidence_hints", ()), weights.get("evidence_potential", 20))
    novelty = score_novelty_vs_history(text, recent_titles, weights.get("novelty_vs_history", 15))
    safety, banned = score_safety_tone(text, rules.get("ban_keywords", ()), weights.get("safety_tone", 5))

    breakdown = {
        "domain_relevance": domain_score,
        "niche_relevance": niche,
        "search_intent_fit": intent,
        "evidence_potential": evidence,
        "novelty_vs_history": novelty,
        "safety_tone": safety,
    }
    total = int(round(sum(breakdown.values())))
    return ScoreResult(
        total=total, breakdown=breakdown, matched_terms=tuple(matched), banned=banned,
        matched_anchors=tuple(anchors),
        missing_anchor=bool(domain.require_anchor and not anchors),
    )
