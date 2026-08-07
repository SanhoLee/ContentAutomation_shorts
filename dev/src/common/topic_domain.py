"""topic_domain.py — the channel's base domain, and what it means to be inside it.

The topic funnel used to have no notion of "the channel is about brains". Seeds
came from `research_categories.json` keywords, and that file lists risk factors
(`혈압`, `고혈압`, `심혈관`, `당뇨`) as first-class keywords because the category's
PubMed query is `"(diabetes OR hypertension) AND cognitive decline"`. Only the
left half of that query ever reached a seed, so autocomplete on `심혈관` returned
straight cardiology, `trend_probe`'s drift gate waved it through (the channel
vocabulary contains `심혈관` too), and `topic_score` had no component that asked
for a brain word at all. `심혈관질환 증상` scored 60+ and `뇌 건강 식단` scored 57.

This module supplies the missing half in two places:

    seed side   — a risk-factor category's seed is probed as `치매 고혈압`, not `고혈압`
    filter side — a candidate with no anchor term is rejected outright

Everything is a plain string computation, so a verdict is reproducible from its
inputs alone — same rule as `topic_score` (no Claude anywhere on this path).

The operator owns the definition: `dev/config/topic_domain.json`. Pointing the
channel at a different field is three lists in that file, not a code change.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

DEFAULT_DOMAIN_PATH = Path(__file__).resolve().parents[2] / "config" / "topic_domain.json"

HANGUL = re.compile(r"[가-힣]")

ROLE_CORE = "core"
ROLE_RISK_FACTOR = "risk_factor"

# The built-in fallback is the real brain-health domain, not an empty permissive
# one: a missing config file must not quietly restore the "심혈관질환 증상 통과"
# behavior this module exists to stop.
DEFAULT_DOMAIN_SPEC: dict[str, Any] = {
    "domain_id": "brain_health",
    "label_ko": "뇌 건강",
    "require_anchor": True,
    "anchor_terms": [
        "뇌", "뇌 건강", "뇌건강", "인지", "인지기능", "기억", "기억력",
        "건망증", "치매", "알츠하이머", "집중력", "판단력", "신경",
        "brain", "cognition", "cognitive", "memory", "dementia", "alzheimer",
    ],
    "seed_anchors": ["치매", "뇌"],
    # research_categories.json mixes scripts (`diabetes`, `hypertension`,
    # `cholesterol`). `치매 diabetes` is a query nobody types, so Latin seeds get
    # a Latin anchor instead.
    "seed_anchors_latin": ["dementia", "brain"],
    "max_anchor_variants": 1,
    "category_roles": {
        "chronic_disease": ROLE_RISK_FACTOR,
        "hearing_vision": ROLE_RISK_FACTOR,
        "stress_cortisol": ROLE_RISK_FACTOR,
        "late_life_depression": ROLE_RISK_FACTOR,
        "social_isolation": ROLE_RISK_FACTOR,
        "diet_nutrition": ROLE_RISK_FACTOR,
        "exercise_cognition": ROLE_RISK_FACTOR,
        "sleep_cognition": ROLE_CORE,
        "mindfulness_meditation": ROLE_CORE,
        "memory_cognition": ROLE_CORE,
    },
    "default_role": ROLE_CORE,
}


@dataclass(frozen=True)
class Domain:
    domain_id: str
    label_ko: str
    require_anchor: bool
    anchor_terms: tuple[str, ...]
    seed_anchors: tuple[str, ...]
    max_anchor_variants: int
    category_roles: Mapping[str, str]
    default_role: str
    seed_anchors_latin: tuple[str, ...] = ()

    def anchors_for(self, seed: str) -> tuple[str, ...]:
        """Anchors in the seed's own script — a mixed-script query like
        `치매 diabetes` gets no autocomplete traffic."""
        if HANGUL.search(seed) or not self.seed_anchors_latin:
            return self.seed_anchors
        return self.seed_anchors_latin


def load_domain(path: str | Path | None = None) -> Domain:
    """Load the domain spec, degrading to DEFAULT_DOMAIN_SPEC on any missing or
    invalid file — same posture as `topic_score.load_rules()`."""
    target = Path(path) if path else DEFAULT_DOMAIN_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    merged: dict[str, Any] = {**DEFAULT_DOMAIN_SPEC, **{k: v for k, v in raw.items() if not k.startswith("_")}}
    return Domain(
        domain_id=str(merged.get("domain_id") or ""),
        label_ko=str(merged.get("label_ko") or ""),
        require_anchor=bool(merged.get("require_anchor", True)),
        anchor_terms=_clean_terms(merged.get("anchor_terms")),
        seed_anchors=_clean_terms(merged.get("seed_anchors")),
        max_anchor_variants=max(1, int(merged.get("max_anchor_variants") or 1)),
        category_roles=dict(merged.get("category_roles") or {}),
        default_role=str(merged.get("default_role") or ROLE_CORE),
        seed_anchors_latin=_clean_terms(merged.get("seed_anchors_latin")),
    )


def _clean_terms(values: Any) -> tuple[str, ...]:
    out: list[str] = []
    for value in values or ():
        text = " ".join(str(value).split()).strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def matched_anchors(text: str, domain: Domain | None = None) -> list[str]:
    """Anchor terms present in `text`.

    Deliberately the same rule as `topic_score._matched()` — lowercase substring
    containment, order preserved, no duplicates. The two must never disagree
    about the same string.
    """
    domain = domain or load_domain()
    lowered = str(text).lower()
    out: list[str] = []
    for term in domain.anchor_terms:
        if term.lower() in lowered and term not in out:
            out.append(term)
    return out


def has_anchor(text: str, domain: Domain | None = None) -> bool:
    return bool(matched_anchors(text, domain))


def role_for(category_id: str | None, domain: Domain | None = None) -> str:
    """A category's role. Seeds with no category (keywords_db / extra origins)
    fall back to `default_role`, since those come from titles the channel has
    already published."""
    domain = domain or load_domain()
    if not category_id:
        return domain.default_role
    return str(domain.category_roles.get(category_id, domain.default_role))


def anchor_variants(
    seed: str, category_id: str | None = None, domain: Domain | None = None,
) -> list[tuple[str, str | None]]:
    """`(probe_seed, anchor)` pairs to actually probe.

    A risk-factor seed is probed anchored (`치매 고혈압`) so the autocomplete pool
    itself is shaped toward the domain. Core seeds, and seeds that already carry
    an anchor word, come back unchanged with `anchor=None` — `치매 치매 예방`
    helps nobody.
    """
    domain = domain or load_domain()
    text = " ".join(str(seed).split()).strip()
    if not text:
        return []
    if role_for(category_id, domain) != ROLE_RISK_FACTOR:
        return [(text, None)]
    anchors = domain.anchors_for(text)
    if has_anchor(text, domain) or not anchors:
        return [(text, None)]
    return [(f"{anchor} {text}", anchor) for anchor in anchors[: domain.max_anchor_variants]]


def anchor_seed(seed: str, category_id: str | None = None, domain: Domain | None = None) -> list[str]:
    """`anchor_variants()` without the anchor labels."""
    return [variant for variant, _ in anchor_variants(seed, category_id, domain)]


def strip_anchor(seed: str, anchor: str | None) -> str:
    """The bare seed behind an anchored one, for the probe fallback path."""
    if not anchor:
        return seed
    prefix = f"{anchor} "
    return seed[len(prefix):] if seed.startswith(prefix) else seed


def describe(domain: Domain | None = None) -> str:
    domain = domain or load_domain()
    gate = "필수" if domain.require_anchor else "선택"
    return f"{domain.label_ko}({domain.domain_id}) · 앵커 {len(domain.anchor_terms)}개 · 앵커 요구 {gate}"


__all__ = [
    "Domain", "ROLE_CORE", "ROLE_RISK_FACTOR", "DEFAULT_DOMAIN_SPEC",
    "load_domain", "matched_anchors", "has_anchor", "role_for",
    "anchor_variants", "anchor_seed", "strip_anchor", "describe",
]
