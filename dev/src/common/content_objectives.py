"""Objective profiles and validation for goal-driven Shorts planning.

All profile values are weights over normalized (0..1) signals.  Claude may
describe qualitative fit, but it never changes these weights at runtime.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Mapping


# research_depth: PubMed 카테고리별 문헌 볼륨 x 최근성 기반 소재 포텐셜 신호
# (see research_signals.py). 2026-07-26에 모든 프로필에 0.08 가중치로 추가되었고,
# 기존 항목들은 (1 - 0.08) 비율로 비례 축소되어 합계 1.0을 유지한다.
OBJECTIVE_PROFILES: dict[str, dict[str, float]] = {
    "subscriber_growth": {
        "net_subscriber_conversion": 0.23,
        "subscriber_conversion": 0.092,
        "average_view_percentage": 0.138,
        "initial_engagement": 0.092,
        "share_rate": 0.092,
        "comment_rate": 0.046,
        "topic_family_conversion": 0.092,
        "series_potential": 0.092,
        "channel_fit": 0.046,
        "research_depth": 0.08,
    },
    "reach": {
        "views_percentile": 0.184,
        "initial_engagement": 0.23,
        "average_view_percentage": 0.138,
        "trend_signal": 0.184,
        "share_rate": 0.092,
        "novelty": 0.092,
        "research_depth": 0.08,
    },
    "retention": {
        "average_view_percentage": 0.322,
        "initial_engagement": 0.184,
        "replay_lift": 0.138,
        "duration_matched_retention": 0.138,
        "narrative_fit": 0.092,
        "trend_signal": 0.046,
        "research_depth": 0.08,
    },
    "share_growth": {
        "share_rate": 0.276,
        "average_view_percentage": 0.138,
        "comment_rate": 0.092,
        "family_relevance": 0.138,
        "actionability": 0.138,
        "topic_trust": 0.092,
        "trend_signal": 0.046,
        "research_depth": 0.08,
    },
    "balanced": {
        "initial_engagement": 0.23,
        "average_view_percentage": 0.276,
        "net_subscriber_conversion": 0.23,
        "share_rate": 0.092,
        "like_rate": 0.046,
        "comment_rate": 0.046,
        "research_depth": 0.08,
    },
}

OBJECTIVE_ALIASES = {
    "subscriber_growth": "subscriber_growth",
    "subscriber": "subscriber_growth",
    "subscribers": "subscriber_growth",
    "구독자": "subscriber_growth",
    "구독자 증가": "subscriber_growth",
    "구독자 증가 강화": "subscriber_growth",
    "reach": "reach",
    "views": "reach",
    "view_growth": "reach",
    "조회수": "reach",
    "조회수 확대": "reach",
    "도달": "reach",
    "retention": "retention",
    "average_view_percentage": "retention",
    "시청률": "retention",
    "평균 시청률": "retention",
    "평균 시청률 개선": "retention",
    "share_growth": "share_growth",
    "shares": "share_growth",
    "공유": "share_growth",
    "공유율": "share_growth",
    "공유율 강화": "share_growth",
    "balanced": "balanced",
    "balance": "balanced",
    "균형": "balanced",
    "균형 성장": "balanced",
}

OBJECTIVE_LABELS = {
    "subscriber_growth": "구독자 증가",
    "reach": "조회수·도달 확대",
    "retention": "평균 시청률 개선",
    "share_growth": "공유율 강화",
    "balanced": "균형 성장",
}


@dataclass(frozen=True)
class ObjectiveConfig:
    objective_type: str
    weights: dict[str, float]
    improvement_target: float = 0.20
    horizon_days: int = 28
    target_value: float | None = None
    target_unit: str | None = None
    priority: int = 1


def normalize_objective_type(value: str | None) -> str:
    key = " ".join(str(value or "balanced").strip().lower().replace("-", "_").split())
    normalized = OBJECTIVE_ALIASES.get(key)
    if normalized is None:
        allowed = ", ".join(OBJECTIVE_PROFILES)
        raise ValueError(f"알 수 없는 목표입니다: {value}. 허용값: {allowed}")
    return normalized


def validate_weights(weights: Mapping[str, Any]) -> dict[str, float]:
    if not weights:
        raise ValueError("목표 가중치가 비어 있습니다.")
    normalized: dict[str, float] = {}
    for name, value in weights.items():
        number = float(value)
        if number < 0:
            raise ValueError(f"가중치는 음수일 수 없습니다: {name}")
        normalized[str(name)] = number
    if abs(sum(normalized.values()) - 1.0) > 1e-9:
        raise ValueError("목표 가중치 합은 1.0이어야 합니다.")
    return normalized


def get_objective_profile(value: str | None) -> dict[str, float]:
    objective_type = normalize_objective_type(value)
    return deepcopy(validate_weights(OBJECTIVE_PROFILES[objective_type]))


def build_objective_config(
    objective_type: str,
    *,
    improvement_target: float = 0.20,
    horizon_days: int = 28,
    target_value: float | None = None,
    target_unit: str | None = None,
    priority: int = 1,
    weights: Mapping[str, Any] | None = None,
) -> ObjectiveConfig:
    normalized = normalize_objective_type(objective_type)
    improvement = float(improvement_target)
    horizon = int(horizon_days)
    priority_value = int(priority)
    if not 0 < improvement <= 10:
        raise ValueError("improvement_target은 0보다 크고 10 이하여야 합니다.")
    if not 1 <= horizon <= 3650:
        raise ValueError("horizon_days는 1~3650 범위여야 합니다.")
    if priority_value < 1:
        raise ValueError("priority는 1 이상이어야 합니다.")
    if target_value is not None and float(target_value) < 0:
        raise ValueError("target_value는 음수일 수 없습니다.")
    return ObjectiveConfig(
        objective_type=normalized,
        weights=validate_weights(weights or OBJECTIVE_PROFILES[normalized]),
        improvement_target=improvement,
        horizon_days=horizon,
        target_value=float(target_value) if target_value is not None else None,
        target_unit=str(target_unit) if target_unit else None,
        priority=priority_value,
    )


def objective_label(value: str | None) -> str:
    return OBJECTIVE_LABELS[normalize_objective_type(value)]


for _name, _profile in OBJECTIVE_PROFILES.items():
    validate_weights(_profile)
