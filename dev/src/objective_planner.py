"""Goal-driven candidate building, Claude critique, and deterministic judging."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from claude_cost import assert_budget, record_usage, usage_total
from content_objectives import (
    ObjectiveConfig,
    build_objective_config,
    get_objective_profile,
    normalize_objective_type,
    objective_label,
)
from research_signals import (
    load_category_signals,
    load_usage_log,
    record_category_usage,
    research_depth_metric,
)
from script_runtime import load_runtime_settings


SCRIPT_DIR = Path(__file__).resolve().parent
_FEEDBACK_SPEC = importlib.util.spec_from_file_location(
    "brain50_objective_feedback", SCRIPT_DIR / "6_youtube_feedback.py"
)
if _FEEDBACK_SPEC is None or _FEEDBACK_SPEC.loader is None:
    raise RuntimeError("6_youtube_feedback.py를 로드할 수 없습니다.")
feedback = importlib.util.module_from_spec(_FEEDBACK_SPEC)
_FEEDBACK_SPEC.loader.exec_module(feedback)

ENUM_SCORE = {"low": 0.25, "medium": 0.50, "high": 0.75}
ENUM_FIELDS = (
    "series_potential", "channel_fit", "family_relevance", "actionability",
    "narrative_fit", "topic_trust",
)
FORMAT_TYPES = (
    "오해반전형", "자가진단형", "사례추적형", "비교형", "연구발견형", "행동챌린지형",
)
HOOK_TYPES = ("반전형", "공감형", "질문형", "경고형", "발견형", "도전형")
EXPLORATION_MODES = ("exploit", "adjacent", "wildcard", "manual")
DECISIONS = ("selected", "limited_test", "manual_review", "rejected")
CRITIC_ENUM_FIELDS = ("duplicate_risk", "overfit_risk", "evidence_risk")
DEFAULT_TOPICS = (
    "수면 중 자주 깨는 이유", "건망증과 기억력 저하의 차이", "아침 혈압 습관",
    "걷기와 뇌 건강", "식후 졸림이 보내는 신호", "물을 마시는 시간",
    "근력과 낙상 예방", "약 복용 시간을 놓쳤을 때", "외로움과 기억력",
    "청력 저하와 인지 건강", "밤늦은 간식과 수면", "하루 한 가지 건강 챌린지",
)
TOPIC_ANGLE_TEMPLATES = {
    "exploit": (
        "효과보다 먼저 확인할 선택 기준",
        "필요한 사람과 피해야 할 사람",
        "광고 문구에서 걸러야 할 오해",
        "생활에서 확인할 실제 변화",
        "전문가에게 물어볼 핵심 질문",
    ),
    "adjacent": (
        "음식과 제품 중 무엇을 먼저 선택할까",
        "복용 시간보다 중요한 생활 조건",
        "약과 함께할 때 놓치기 쉬운 점",
        "가족과 함께 점검할 안전 기준",
    ),
    "wildcard": (
        "일주일 동안 기록해 볼 한 가지",
        "상식을 뒤집어 확인하는 작은 실험",
    ),
}
TOPIC_FAMILY_RULES = (
    ("보조식품", ("보조 식품", "보조식품", "영양제", "비타민", "미네랄")),
    ("수면", ("수면", "잠", "불면", "새벽")),
    ("기억력", ("기억력", "건망증", "치매", "인지")),
    ("혈압", ("혈압", "심혈관")),
    ("혈당", ("혈당", "당뇨", "식후")),
    ("운동", ("걷기", "근력", "운동", "낙상")),
    ("영양", ("음식", "식품", "식사", "간식", "음료")),
    # 2026-07-26 research_categories.json 확장 카테고리 반영.
    # 기존 규칙과 겹치는 키워드(수면/기억력/혈압/혈당/운동/영양)는 위에서
    # 이미 매칭되므로 순서상 아래로 배치해 우선순위 충돌을 피한다.
    ("스트레스", ("스트레스", "코티솔")),
    ("명상", ("명상", "마음챙김")),
    ("사회적고립", ("외로움", "고립")),
    ("우울", ("우울", "무기력")),
    ("청력시각", ("청력", "난청", "시력", "시각")),
)
CANDIDATE_SOURCE_TARGETS = (
    ("performance_exploit",) * 5
    + ("adjacent",) * 3
    + ("trend",) * 3
    + ("wildcard",)
)
SOURCE_EXPLORATION_MODE = {
    "performance_exploit": "exploit",
    "adjacent": "adjacent",
    "trend": "adjacent",
    "wildcard": "wildcard",
}
# Preflight gate score needed before Claude planner is invoked.
# Auto-discovered candidates (no seed) rely on channel evidence, so a higher
# bar is appropriate. A user-supplied seed is an explicit intent signal —
# the LLM should get a chance to interpret it even when local deterministic
# scoring (zero confidence, no evidence_refs) can't score it highly on its
# own. Critic review still runs afterward, so this does not skip validation.
PREFLIGHT_THRESHOLD_AUTO = 50.0
PREFLIGHT_THRESHOLD_MANUAL_SEED = 20.0


def _channel_evidence_profile(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("""
        SELECT COUNT(DISTINCT s.video_id) AS count
        FROM performance_snapshots s
        WHERE EXISTS (
            SELECT 1 FROM videos v WHERE v.video_id=s.video_id
        )
          AND NOT EXISTS (
            SELECT 1 FROM analytics a WHERE a.video_id=s.video_id
              AND a.creator_content_type IS NOT NULL AND a.creator_content_type!='SHORTS'
          )
    """).fetchone()
    count = int((row or {})["count"] or 0) if row else 0
    reliability = float(feedback.cohort_reliability(count))
    if count < feedback.MATURITY_LEARNING_VIDEOS:
        stage = "early"
        targets = (("performance_exploit",) * 2 + ("adjacent",) * 4 + ("trend",) * 4 + ("wildcard",) * 2)
    elif count < feedback.MATURITY_GROWING_VIDEOS:
        stage = "learning"
        targets = (("performance_exploit",) * 3 + ("adjacent",) * 4 + ("trend",) * 3 + ("wildcard",) * 2)
    elif count < feedback.MATURITY_STABLE_VIDEOS:
        stage = "growing"
        targets = (("performance_exploit",) * 4 + ("adjacent",) * 3 + ("trend",) * 3 + ("wildcard",) * 2)
    else:
        stage = "stable"
        targets = CANDIDATE_SOURCE_TARGETS
    return {
        "sample_count": count,
        "reliability": reliability,
        "stage": stage,
        "source_targets": targets,
    }


class PlannerValidationError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _compact_words(value: str) -> set[str]:
    return feedback.normalize_keywords(str(value or ""))


def _jaccard_topic(left: str, right: str) -> float:
    return feedback.jaccard(_compact_words(left), _compact_words(right))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def create_objective(conn: sqlite3.Connection, config: ObjectiveConfig) -> int:
    now = utc_now()
    cursor = conn.execute(
        """INSERT INTO objectives (
            objective_type, target_value, target_unit, improvement_target,
            horizon_days, priority, weights_json, constraints_json,
            status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)""",
        (
            config.objective_type, config.target_value, config.target_unit,
            config.improvement_target, config.horizon_days, config.priority,
            _json(config.weights), _json({}), now, now,
        ),
    )
    return int(cursor.lastrowid)


def _latest_objective(conn: sqlite3.Connection, config: ObjectiveConfig) -> int:
    row = conn.execute(
        """SELECT objective_id, weights_json, improvement_target, horizon_days
           FROM objectives WHERE objective_type=? AND status='active'
           ORDER BY objective_id DESC LIMIT 1""",
        (config.objective_type,),
    ).fetchone()
    if row:
        same = (
            json.loads(row["weights_json"]) == config.weights
            and float(row["improvement_target"] or 0.20) == config.improvement_target
            and int(row["horizon_days"] or 28) == config.horizon_days
        )
        if same:
            return int(row["objective_id"])
    return create_objective(conn, config)


def _preferred_snapshot_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("""
        WITH ranked AS (
            SELECT s.*,
              CASE s.window_name WHEN 'D28' THEN 1 WHEN 'D7' THEN 2
                   WHEN 'D1_APPROX' THEN 3 ELSE 4 END AS window_rank,
              ROW_NUMBER() OVER (
                PARTITION BY s.video_id
                ORDER BY CASE s.window_name WHEN 'D28' THEN 1 WHEN 'D7' THEN 2
                         WHEN 'D1_APPROX' THEN 3 ELSE 4 END,
                         s.fetched_at DESC
              ) AS row_number
            FROM performance_snapshots s
        )
        SELECT r.*, v.title, v.published_at, v.duration_seconds,
               v.upload_jst_hour, v.upload_jst_weekday, v.upload_month,
               v.hours_since_previous_upload,
               f.topic_family, f.format_type, f.hook_type
        FROM ranked r JOIN videos v ON v.video_id=r.video_id
        LEFT JOIN content_features f ON f.video_id=r.video_id
        WHERE r.row_number=1
        ORDER BY v.published_at DESC
    """).fetchall()


def _existing_titles(conn: sqlite3.Connection) -> list[str]:
    values = [str(row["title"]) for row in conn.execute("SELECT title FROM videos")]
    values.extend(
        str(row["topic"])
        for row in conn.execute("SELECT topic FROM video_jobs WHERE topic!=''")
    )
    for row in conn.execute("""
        SELECT selected_candidate_json FROM planning_runs
        WHERE selected_candidate_json IS NOT NULL
        ORDER BY plan_id DESC LIMIT 100
    """):
        try:
            selected = json.loads(row["selected_candidate_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        for key in ("topic", "title"):
            value = str(selected.get(key) or "").strip()
            if value:
                values.append(value)
    return list(dict.fromkeys(values))


def _candidate_topic(base: str, mode: str, index: int) -> str:
    base = " ".join(str(base or "").split()).strip()
    angles = TOPIC_ANGLE_TEMPLATES[mode]
    angle = angles[index % len(angles)]
    return f"{base}: {angle}" if base else angle


def _topic_family(topic: str, seed_topic: str | None = None) -> str:
    text = " ".join((seed_topic or "", topic)).lower()
    for family, keywords in TOPIC_FAMILY_RULES:
        if any(keyword in text for keyword in keywords):
            return family
    words = sorted(_compact_words(seed_topic or topic))
    return words[0] if words else "기타"


# Default containment cutoff: if 2+ words overlap and cover 80%+ of the
# shorter side, treat as a near-duplicate even when Jaccard similarity is
# below threshold. Multi-word manual seeds ("복용 시간 생활 조건" style
# sentences) share more words with existing titles by construction, so this
# cutoff is raised for manual-seed candidates to avoid false-positive blocks
# on topics that are merely related, not duplicate.
CONTAINMENT_CUTOFF_DEFAULT = 0.80
CONTAINMENT_CUTOFF_MANUAL_SEED = 0.90


def _topic_duplicate_info(
    topic: str,
    existing_titles: Sequence[str],
    threshold: float,
    *,
    containment_cutoff: float = CONTAINMENT_CUTOFF_DEFAULT,
) -> dict[str, Any]:
    topic_words = _compact_words(topic)
    best = {"similarity": 0.0, "containment": 0.0, "title": "", "common_keywords": []}
    normalized_topic = re.sub(r"\s+", "", topic).lower()
    for title in existing_titles:
        title_words = _compact_words(title)
        common = topic_words & title_words
        similarity = feedback.jaccard(topic_words, title_words)
        containment = len(common) / min(len(topic_words), len(title_words)) if topic_words and title_words else 0.0
        exact = normalized_topic == re.sub(r"\s+", "", title).lower()
        rank = max(similarity, containment if len(common) >= 2 else 0.0, 1.0 if exact else 0.0)
        current_rank = max(best["similarity"], best["containment"] if len(best["common_keywords"]) >= 2 else 0.0)
        if rank > current_rank:
            best = {
                "similarity": similarity,
                "containment": containment,
                "title": title,
                "common_keywords": sorted(common),
                "exact": exact,
            }
    best.setdefault("exact", False)
    best["blocked"] = bool(
        best["exact"]
        or best["similarity"] >= threshold
        or (len(best["common_keywords"]) >= 2 and best["containment"] >= containment_cutoff)
    )
    return best


def _transfer_pattern_metrics(normalized: Mapping[str, Any], reliability: float = 0.35) -> dict[str, float]:
    return {
        name: 0.5 + (float(value) - 0.5) * reliability
        for name, value in (normalized.get("metrics") or {}).items()
        if value is not None
    }


def build_candidate_pool(
    conn: sqlite3.Connection,
    *,
    objective_type: str,
    seed_topic: str | None = None,
    trend_candidates: Sequence[Mapping[str, Any] | str] | None = None,
    candidate_count: int = 12,
    rejected_duplicates: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build a 5/3/3/1 evidence, adjacent, trend, wildcard candidate pool."""
    objective_type = normalize_objective_type(objective_type)
    manual_seed = bool(str(seed_topic or "").strip())
    containment_cutoff = (
        CONTAINMENT_CUTOFF_MANUAL_SEED if manual_seed else CONTAINMENT_CUTOFF_DEFAULT
    )
    rows = _preferred_snapshot_rows(conn)
    existing_titles = _existing_titles(conn)
    evidence_profile = _channel_evidence_profile(conn)
    research_signals = load_category_signals()
    research_usage = load_usage_log() if research_signals else {}
    source_targets = tuple(evidence_profile["source_targets"])
    strictness = os.environ.get("YOUTUBE_FEEDBACK_STRICTNESS", "balanced")
    duplicate_threshold = float(feedback.adaptive_topic_thresholds(conn, strictness)["duplicate"])
    evidence: list[tuple[float, sqlite3.Row, dict[str, Any]]] = []
    for row in rows:
        normalized = feedback.normalized_snapshot_metrics(conn, row["video_id"], row["window_name"])
        outcome = feedback.classify_exposure_quality(conn, row["video_id"], row["window_name"])
        quality = float(outcome.get("quality_score") or 0.5)
        priority = quality + (0.25 if outcome.get("classification") == "hidden_success" else 0.0)
        evidence.append((priority, row, {**normalized, "classification": outcome.get("classification")}))
    evidence.sort(key=lambda item: item[0], reverse=True)

    trend_values: list[dict[str, Any]] = []
    for value in trend_candidates or ():
        if isinstance(value, Mapping):
            topic = str(value.get("keyword") or value.get("topic") or "").strip()
            sources = list(value.get("sources") or [])
        else:
            topic, sources = str(value).strip(), ["suggest"]
        if topic:
            repeat_row = conn.execute(
                "SELECT COUNT(DISTINCT observed_date) AS days FROM trend_observations WHERE topic=?",
                (topic,),
            ).fetchone()
            trend_values.append({
                "topic": topic, "sources": sources,
                "repeat_days": int(repeat_row["days"] or 0) if repeat_row else 0,
            })

    discovery_bases = [str(seed_topic).strip()] if str(seed_topic or "").strip() else list(DEFAULT_TOPICS)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index in range(max(candidate_count * 8, 48)):
        if len(candidates) >= candidate_count:
            break
        slot = len(candidates)
        candidate_source = (
            source_targets[slot]
            if slot < len(source_targets)
            else "wildcard"
        )
        mode = SOURCE_EXPLORATION_MODE[candidate_source]
        trend_slot = candidate_source == "trend"
        # Trend candidates are independent observations. Attaching a random
        # channel video here would falsely present unrelated metrics as proof.
        evidence_item = evidence[index % len(evidence)] if evidence and not trend_slot else None
        if trend_slot:
            if trend_values:
                trend = trend_values[index % len(trend_values)]
                topic = trend["topic"]
                sources = trend["sources"]
                repeat_days = int(trend.get("repeat_days") or 0)
            else:
                base = discovery_bases[index % len(discovery_bases)]
                topic = _candidate_topic(f"{base} 관련 검색", "adjacent", index)
                sources = ["trend_fallback"]
                repeat_days = 0
        else:
            base = discovery_bases[index % len(discovery_bases)]
            topic = _candidate_topic(base, mode, index)
            sources = ["channel_pattern"] if evidence_item else ["exploration"]
            repeat_days = 0
        key = re.sub(r"\s+", "", topic).lower()
        if not key or key in seen:
            continue
        duplicate = _topic_duplicate_info(
            topic, existing_titles, duplicate_threshold, containment_cutoff=containment_cutoff
        )
        if duplicate["blocked"]:
            if rejected_duplicates is not None:
                rejected_duplicates.append({"topic": topic, **duplicate})
            continue
        seen.add(key)
        closest = float(duplicate["similarity"])
        if evidence_item:
            _, source_row, normalized = evidence_item
            metrics = _transfer_pattern_metrics(normalized)
            evidence_refs = [
                f"video:{source_row['video_id']}",
                f"metric:{source_row['window_name']}:{source_row['video_id']}",
            ]
            confidence = float(normalized.get("confidence") or 0.0) * 0.70
            classification = normalized.get("classification")
            confounders = ["pattern_transfer_only"]
            if classification == "exposure_luck":
                confounders.append("shorts_feed_exposure")
            if normalized.get("confidence_level") == "low":
                confounders.append("small_sample")
            if source_row["upload_jst_hour"] is not None:
                confounders.append("upload_time_observed_not_causal")
        else:
            metrics, evidence_refs, confidence, classification = {}, [], 0.0, "insufficient_data"
            confounders = ["small_sample"]
        family = _topic_family(topic, seed_topic)
        source_count = max(1, len(set(sources)))
        research_depth, research_debug = (
            research_depth_metric(family, research_signals, research_usage)
            if research_signals else (0.5, {"matched": False, "reason": "no_category_data"})
        )
        metrics.update({
            "trend_signal": min(1.0, 0.30 + 0.18 * source_count + 0.04 * min(repeat_days, 5)) if trend_slot else 0.5,
            "novelty": max(0.0, 1.0 - closest),
            "research_depth": research_depth,
        })
        candidates.append({
            "candidate_id": f"cand_{len(candidates) + 1:02d}",
            "topic": topic,
            "topic_family": family,
            "exploration_mode": mode,
            "candidate_source": candidate_source,
            "sources": sources,
            "evidence_refs": evidence_refs,
            "source_classification": classification,
            "normalized_metrics": metrics,
            "research_category": research_debug,
            "confidence": confidence,
            "channel_sample_count": evidence_profile["sample_count"],
            "channel_reliability": round(float(evidence_profile["reliability"]), 6),
            "channel_maturity": evidence_profile["stage"],
            "duplicate_similarity": closest,
            "duplicate_containment": duplicate["containment"],
            "duplicate_threshold": duplicate_threshold,
            "closest_existing_title": duplicate["title"],
            "confounders": confounders,
        })
    return candidates


def valid_evidence_refs(candidates: Sequence[Mapping[str, Any]]) -> set[str]:
    """Return only evidence references that were actually shown to the model."""
    return {
        str(ref)
        for candidate in candidates
        for ref in candidate.get("evidence_refs", [])
    }


def _unsupported_numeric_claim(candidate: Mapping[str, Any]) -> bool:
    fields = ("angle", "reason", "topic")
    text = " ".join(str(candidate.get(field) or "") for field in fields)
    return bool(re.search(r"\d+(?:\.\d+)?\s*(?:%|퍼센트|배|명|개월|년)", text))


def validate_planner_output(
    output: Mapping[str, Any],
    input_candidates: Sequence[Mapping[str, Any]],
    *,
    valid_refs: Iterable[str] | None = None,
    existing_video_ids: Iterable[str] | None = None,
    existing_titles: Iterable[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(output, Mapping) or not isinstance(output.get("candidates"), list):
        raise PlannerValidationError("Planner JSON schema가 올바르지 않습니다.")
    candidate_map = {str(item["candidate_id"]): item for item in input_candidates}
    allowed_refs = None if valid_refs is None else set(valid_refs)
    allowed_videos = None if existing_video_ids is None else set(existing_video_ids)
    titles = {str(title).strip().lower() for title in (existing_titles or ())}
    sanitized = []
    skipped = []
    seen = set()
    for raw in output["candidates"]:
        if not isinstance(raw, Mapping):
            raise PlannerValidationError("Planner 후보는 JSON object여야 합니다.")
        candidate_id = str(raw.get("candidate_id") or "")
        if candidate_id not in candidate_map:
            raise PlannerValidationError(f"입력에 없는 candidate_id입니다: {candidate_id}")
        if candidate_id in seen:
            raise PlannerValidationError(f"중복 candidate_id입니다: {candidate_id}")
        seen.add(candidate_id)
        topic = str(raw.get("topic") or candidate_map[candidate_id].get("topic") or "").strip()
        if not topic:
            raise PlannerValidationError("Planner topic이 비어 있습니다.")
        if topic.lower() in titles:
            raise PlannerValidationError("기존 영상 제목을 그대로 복사한 후보입니다.")
        refs = [str(ref) for ref in raw.get("evidence_refs") or []]
        for ref in refs:
            if allowed_refs is not None and ref not in allowed_refs:
                raise PlannerValidationError(f"입력 데이터에 없는 evidence_ref입니다: {ref}")
            if (
                ref.startswith("video:")
                and allowed_videos is not None
                and ref.split(":", 1)[1] not in allowed_videos
            ):
                raise PlannerValidationError(f"DB에 없는 video_id입니다: {ref}")
        # Enum/format/hook 값 오류는 데이터 무결성(환각·근거조작) 문제가 아니라
        # 단순 포맷 실수이므로, 해당 후보만 skip하고 전체 응답은 계속 처리한다.
        # candidate_id·evidence_ref·topic 복제 등 신뢰성 관련 위반은 위에서 여전히
        # 즉시 전체 실패로 처리된다.
        enum_violation = next(
            (
                f"허용되지 않은 enum입니다: {field}={raw[field]}"
                for field in ENUM_FIELDS
                if field in raw and str(raw[field]) not in ENUM_SCORE
            ),
            None,
        )
        if enum_violation:
            skipped.append({"candidate_id": candidate_id, "reason": enum_violation})
            continue
        if raw.get("format_type") and str(raw["format_type"]) not in FORMAT_TYPES:
            skipped.append({
                "candidate_id": candidate_id,
                "reason": f"허용되지 않은 format_type입니다: {raw['format_type']}",
            })
            continue
        if raw.get("hook_type") and str(raw["hook_type"]) not in HOOK_TYPES:
            skipped.append({
                "candidate_id": candidate_id,
                "reason": f"허용되지 않은 hook_type입니다: {raw['hook_type']}",
            })
            continue
        risk_flags = [str(item) for item in raw.get("risk_flags") or []]
        if _unsupported_numeric_claim(raw) and "unsupported_claim" not in risk_flags:
            risk_flags.append("unsupported_claim")
        # Numeric scores from AI are intentionally not copied.
        allowed = {
            "candidate_id", "topic", "topic_family", "angle", "format_type", "hook_type",
            "emotion_curve", "series_key", "series_potential", "channel_fit",
            "family_relevance", "actionability", "narrative_fit", "topic_trust",
            "evidence_refs", "risk_flags", "search_intent", "core_message", "title",
            "thumbnail_text", "cta_next", "main_keyword", "sub_keywords",
        }
        item = {key: raw[key] for key in allowed if key in raw}
        item.update({"candidate_id": candidate_id, "topic": topic, "risk_flags": risk_flags})
        sanitized.append(item)
    if not sanitized:
        if skipped:
            reasons = "; ".join(f"{s['candidate_id']}: {s['reason']}" for s in skipped)
            raise PlannerValidationError(f"모든 후보가 format/hook 오류로 제외되었습니다: {reasons}")
        raise PlannerValidationError("Planner가 유효 후보를 반환하지 않았습니다.")
    return {"candidates": sanitized, "skipped_candidates": skipped}


def validate_critic_output(
    output: Mapping[str, Any],
    candidate_ids: Iterable[str],
    *,
    valid_refs: Iterable[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(output, Mapping) or not isinstance(output.get("reviews"), list):
        raise PlannerValidationError("Critic JSON schema가 올바르지 않습니다.")
    allowed_ids = set(candidate_ids)
    allowed_refs = None if valid_refs is None else set(valid_refs)
    reviews = []
    seen = set()
    for raw in output["reviews"]:
        if not isinstance(raw, Mapping):
            raise PlannerValidationError("Critic review는 JSON object여야 합니다.")
        candidate_id = str(raw.get("candidate_id") or "")
        if candidate_id not in allowed_ids:
            raise PlannerValidationError(f"Critic이 알 수 없는 후보를 참조했습니다: {candidate_id}")
        if candidate_id in seen:
            raise PlannerValidationError(f"Critic이 후보를 중복 검토했습니다: {candidate_id}")
        seen.add(candidate_id)
        for field in CRITIC_ENUM_FIELDS:
            if str(raw.get(field) or "medium") not in ENUM_SCORE:
                raise PlannerValidationError(f"허용되지 않은 Critic enum입니다: {field}")
        refs = [str(ref) for ref in raw.get("contradicting_refs") or []]
        if allowed_refs is not None and any(ref not in allowed_refs for ref in refs):
            raise PlannerValidationError("Critic이 입력에 없는 반증 근거를 참조했습니다.")
        action = str(raw.get("recommended_action") or "limited_test")
        if action not in DECISIONS:
            raise PlannerValidationError(f"허용되지 않은 recommended_action입니다: {action}")
        reviews.append({
            "candidate_id": candidate_id,
            "contradicting_refs": refs,
            "confounders": [str(item) for item in raw.get("confounders") or []],
            "duplicate_risk": str(raw.get("duplicate_risk") or "medium"),
            "overfit_risk": str(raw.get("overfit_risk") or "medium"),
            "evidence_risk": str(raw.get("evidence_risk") or "medium"),
            "recommended_action": action,
            "reason": str(raw.get("reason") or ""),
        })
    return {"reviews": reviews}


def _planner_prompt(
    config: ObjectiveConfig,
    candidates: Sequence[Mapping[str, Any]],
    hypotheses: Sequence[Mapping[str, Any]],
) -> str:
    compact_candidates = [{
        "candidate_id": item["candidate_id"],
        "topic": item["topic"],
        "topic_family": item.get("topic_family"),
        "exploration_mode": item.get("exploration_mode"),
        "candidate_source": item.get("candidate_source"),
        "evidence_refs": item.get("evidence_refs") or [],
        "confidence": item.get("confidence", 0),
        "duplicate_similarity": item.get("duplicate_similarity", 0),
        "normalized_metrics": item.get("normalized_metrics") or {},
    } for item in candidates]
    compact_hypotheses = [{
        "statement": item.get("statement"),
        "confidence": item.get("confidence"),
        "status": item.get("status"),
    } for item in hypotheses[:5]]
    return f"""목표 기반 YouTube Shorts 후보를 정성적으로 설계하세요.
목표: {config.objective_type} ({objective_label(config.objective_type)})
후보 데이터: {_json(compact_candidates)}
활성 전략 가설: {_json(compact_hypotheses)}

규칙:
- 숫자 점수, 검색량, 성과 수치, 인과관계를 새로 만들지 마세요.
- candidate_id와 evidence_refs는 입력에 있는 값만 사용하세요.
- 입력 주제의 핵심 대상을 바꾸지 말고 기존 제목을 복사하지 마세요.
- 정성 enum은 low/medium/high만 사용하세요.
- format_type은 반드시 다음 중 하나여야 합니다 (다른 값 절대 사용 금지):
  {list(FORMAT_TYPES)}
- hook_type은 반드시 다음 중 하나여야 합니다 (다른 값 절대 사용 금지):
  {list(HOOK_TYPES)}
- candidates 배열을 가진 JSON 객체만 출력하세요.
각 후보 필드: candidate_id, topic, topic_family, angle, format_type, hook_type,
series_potential, channel_fit, family_relevance, actionability, narrative_fit,
topic_trust, evidence_refs, risk_flags."""


def _critic_prompt(candidates: Sequence[Mapping[str, Any]]) -> str:
    compact_candidates = [{
        "candidate_id": item.get("candidate_id"),
        "topic": item.get("topic"),
        "angle": item.get("angle"),
        "format_type": item.get("format_type"),
        "hook_type": item.get("hook_type"),
        "evidence_refs": item.get("evidence_refs") or [],
        "confidence": item.get("confidence", 0),
        "duplicate_similarity": item.get("duplicate_similarity", 0),
        "confounders": item.get("confounders") or [],
    } for item in candidates]
    return f"""아래 상위 Shorts 후보가 틀릴 수 있는 이유를 찾으세요. 후보를 지지하지 마세요.
조회수 우연, Shorts Feed 노출 편향, 업로드 시간, 영상 길이, 계절성, 작은 표본,
주제 중복, 제목-내용 불일치, 채널 과적합을 검토하세요.
후보: {_json(compact_candidates)}
숫자 점수를 만들지 말고 reviews 배열을 가진 JSON만 출력하세요.
각 review 필드: candidate_id, contradicting_refs, confounders, duplicate_risk,
overfit_risk, evidence_risk, recommended_action, reason.
위험 enum은 low/medium/high, recommended_action은 selected/limited_test/rejected 중 하나입니다."""


def _extract_claude_json(data: Mapping[str, Any]) -> dict[str, Any]:
    if str(data.get("stop_reason") or "") == "max_tokens":
        raise PlannerValidationError("Claude 응답이 토큰 한도에서 잘려 JSON이 완성되지 않았습니다.")
    text = "".join(
        block.get("text", "")
        for block in data.get("content", [])
        if block.get("type") == "text"
    )
    text = re.sub(r"^```(?:json)?", "", text.strip()).rstrip("`").strip()
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PlannerValidationError(f"Claude JSON 파싱 실패: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PlannerValidationError("Claude 응답 최상위 값은 JSON object여야 합니다.")
    return parsed


def call_claude_json(
    prompt: str,
    *,
    model: str,
    max_tokens: int,
    stage: str,
    job_id: str,
    plan_id: int | None = None,
) -> dict[str, Any]:
    import requests

    settings = load_runtime_settings()
    assert_budget(
        job_id=job_id, job_budget_usd=settings.claude_job_budget_usd,
        daily_budget_usd=settings.claude_daily_budget_usd,
    )
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY가 없어 deterministic fallback을 사용합니다.")
    usage_path = Path(os.environ.get("WORK_DIR", ".")) / "claude_usage.jsonl"
    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key, "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]},
            timeout=settings.claude_timeout,
        )
    except Exception:
        record_usage(
            stage, model, {}, jsonl_path=usage_path, job_id=job_id,
            plan_id=plan_id, success=False,
        )
        raise
    if response.status_code >= 400:
        try:
            failed_data = response.json()
        except Exception:
            failed_data = {}
        failed_data["_request_id"] = response.headers.get("request-id")
        record_usage(
            stage, model, failed_data, jsonl_path=usage_path, job_id=job_id,
            plan_id=plan_id, success=False,
        )
    response.raise_for_status()
    data = response.json()
    data["_request_id"] = response.headers.get("request-id")
    try:
        parsed = _extract_claude_json(data)
    except Exception:
        record_usage(
            stage, model, data, jsonl_path=usage_path, job_id=job_id,
            plan_id=plan_id, success=False,
        )
        raise
    record_usage(
        stage, model, data, jsonl_path=usage_path, job_id=job_id,
        plan_id=plan_id, success=True,
    )
    return parsed


def _qualitative_score(planner: Mapping[str, Any]) -> float:
    values = [ENUM_SCORE[str(planner[field])] for field in ENUM_FIELDS if field in planner]
    return statistics_mean(values, default=0.5)


def statistics_mean(values: Sequence[float], default: float = 0.5) -> float:
    return sum(values) / len(values) if values else default


def _score_blend_weights(channel_reliability: float) -> dict[str, float]:
    reliability = max(0.0, min(1.0, float(channel_reliability)))
    metric = 0.35 + 0.35 * reliability
    trend_novelty = 0.25 - 0.15 * reliability
    qualitative = 1.0 - metric - trend_novelty
    return {
        "metric": metric,
        "qualitative": qualitative,
        "trend_novelty": trend_novelty,
    }


def score_candidate(
    candidate: Mapping[str, Any],
    planner: Mapping[str, Any] | None,
    objective_type: str,
) -> dict[str, Any]:
    planner = planner or {}
    weights = get_objective_profile(objective_type)
    metrics = dict(candidate.get("normalized_metrics") or {})
    qualitative_names = set(ENUM_FIELDS)
    metric_components = [
        (weight, float(metrics.get(name, 0.5)))
        for name, weight in weights.items() if name not in qualitative_names
    ]
    metric_weight = sum(weight for weight, _ in metric_components)
    metric_score = (
        sum(weight * value for weight, value in metric_components) / metric_weight
        if metric_weight else 0.5
    )
    qualitative_score = _qualitative_score(planner)
    trend_novelty = statistics_mean([
        float(metrics.get("trend_signal", 0.5)), float(metrics.get("novelty", 0.5))
    ])
    blend = _score_blend_weights(float(candidate.get("channel_reliability") or 0.0))
    base_score = 100.0 * (
        metric_score * blend["metric"]
        + qualitative_score * blend["qualitative"]
        + trend_novelty * blend["trend_novelty"]
    )
    return {
        "metric_score": round(metric_score, 6),
        "qualitative_score": round(qualitative_score, 6),
        "trend_novelty_score": round(trend_novelty, 6),
        "metric_weight": round(blend["metric"], 6),
        "qualitative_weight": round(blend["qualitative"], 6),
        "trend_novelty_weight": round(blend["trend_novelty"], 6),
        "base_score": round(base_score, 4),
    }


def exploration_target(job_id: str) -> str:
    value = int(hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:8], 16) % 10
    if value < 7:
        return "exploit"
    if value < 9:
        return "adjacent"
    return "wildcard"


def judge_candidate(
    candidate: Mapping[str, Any],
    score: Mapping[str, Any],
    critic: Mapping[str, Any] | None,
    *,
    desired_exploration: str,
    stale_strategy: bool = False,
) -> dict[str, Any]:
    critic = critic or {
        "duplicate_risk": "medium", "overfit_risk": "medium", "evidence_risk": "medium",
        "recommended_action": "limited_test", "reason": "Critic 실패: 중간 위험 적용",
    }
    duplicate_penalty = min(15.0, max(
        15.0 * float(candidate.get("duplicate_similarity") or 0.0),
        10.0 if candidate.get("format_hook_repeat") else 0.0,
    ))
    critic_values = [ENUM_SCORE.get(str(critic.get(field) or "medium"), 0.5) for field in CRITIC_ENUM_FIELDS]
    critic_risk_penalty = min(10.0, 10.0 * statistics_mean(critic_values))
    confidence = float(candidate.get("confidence") or 0.0)
    low_confidence_penalty = min(15.0, 15.0 * max(0.0, 0.6 - confidence) / 0.6)
    stale_penalty = 5.0 if stale_strategy else 0.0
    mode = str(candidate.get("exploration_mode") or "exploit")
    exploration_bonus = 0.0
    if mode == desired_exploration:
        exploration_bonus = 5.0 if mode == "wildcard" else 3.0 if mode == "adjacent" else 0.0
    adjusted = float(score["base_score"]) - duplicate_penalty - critic_risk_penalty
    adjusted -= low_confidence_penalty + stale_penalty
    adjusted += exploration_bonus
    if adjusted >= 70.0 and confidence >= 0.6 and critic.get("recommended_action") != "rejected":
        decision = "selected"
    elif adjusted >= 55.0:
        decision = "limited_test"
    elif confidence < 0.6:
        decision = "manual_review"
    else:
        decision = "rejected"
    return {
        **score,
        "adjusted_score": round(adjusted, 4),
        "confidence": round(confidence, 6),
        "decision": decision,
        "penalties": {
            "duplicate": round(duplicate_penalty, 4),
            "critic_risk": round(critic_risk_penalty, 4),
            "low_confidence": round(low_confidence_penalty, 4),
            "stale_strategy": stale_penalty,
        },
        "exploration_bonus": exploration_bonus,
    }


def _default_planner_item(candidate: Mapping[str, Any]) -> dict[str, Any]:
    topic = str(candidate["topic"])
    mode = str(candidate.get("exploration_mode") or "exploit")
    formats = {"exploit": "오해반전형", "adjacent": "자가진단형", "wildcard": "행동챌린지형"}
    hooks = {"exploit": "반전형", "adjacent": "질문형", "wildcard": "도전형"}
    return {
        "candidate_id": candidate["candidate_id"], "topic": topic,
        "topic_family": candidate.get("topic_family") or "기타",
        "angle": "기존 결론을 복제하지 않고 생활 속 판단 기준에 초점",
        "format_type": formats.get(mode, "오해반전형"), "hook_type": hooks.get(mode, "공감형"),
        "emotion_curve": ["공감", "의외성", "이해", "안심", "행동"],
        "series_key": candidate.get("topic_family") or "건강 습관",
        "series_potential": "medium", "channel_fit": "medium",
        "family_relevance": "medium", "actionability": "medium",
        "narrative_fit": "medium", "topic_trust": "medium",
        "evidence_refs": list(candidate.get("evidence_refs") or []), "risk_flags": [],
    }


def _optimistic_critic(candidate_id: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "duplicate_risk": "low", "overfit_risk": "low", "evidence_risk": "low",
        "recommended_action": "limited_test", "reason": "로컬 사전 평가",
    }


def _local_candidate_rows(
    candidates: Sequence[dict[str, Any]],
    objective_type: str,
    job_id: str,
) -> list[dict[str, Any]]:
    rows = []
    for candidate in candidates:
        planner = _default_planner_item(candidate)
        score = score_candidate(candidate, planner, objective_type)
        judgment = judge_candidate(
            candidate, score, _optimistic_critic(str(candidate["candidate_id"])),
            desired_exploration=exploration_target(job_id),
        )
        rows.append({"candidate": candidate, "planner": planner, "score": score, "judgment": judgment})
    return sorted(rows, key=lambda item: item["judgment"]["adjusted_score"], reverse=True)


def _topic_plan(selected: Mapping[str, Any], plan_id: int, objective_id: int) -> dict[str, Any]:
    candidate = selected["candidate"]
    planner = selected["planner"]
    judgment = selected["judgment"]
    topic = str(planner.get("topic") or candidate["topic"])
    main_keyword = str(planner.get("main_keyword") or topic.split("-")[0].strip())[:24]
    title = str(planner.get("title") or topic)[:60]
    format_type = str(planner.get("format_type") or "오해반전형")
    return {
        "topic": topic,
        "main_keyword": main_keyword,
        "sub_keywords": list(planner.get("sub_keywords") or list(_compact_words(topic))[:2]),
        "search_intent": str(planner.get("search_intent") or "내 상황에 맞는 판단 기준이 궁금함"),
        "hook_type": str(planner.get("hook_type") or "반전형"),
        "title": title,
        "search_title_format": format_type,
        "core_message": str(planner.get("core_message") or "작은 신호를 알고 부담 없는 실천부터 시작하세요"),
        "thumbnail_text": list(planner.get("thumbnail_text") or [main_keyword[:14]]),
        "frame_header": {"title": main_keyword[:9], "subtitle": title[:18]},
        "cta_next": str(planner.get("cta_next") or f"{planner.get('series_key') or main_keyword} 다음 편"),
        "objective": {
            "type": selected["objective_type"], "objective_id": objective_id,
            "plan_id": plan_id, "selection_mode": candidate.get("exploration_mode"),
            "confidence": judgment["confidence"], "decision": judgment["decision"],
            "base_score": judgment["base_score"], "adjusted_score": judgment["adjusted_score"],
            "duplicate_similarity": candidate.get("duplicate_similarity", 0),
            "duplicate_threshold": candidate.get("duplicate_threshold"),
            "closest_existing_title": candidate.get("closest_existing_title") or "",
            "evidence_refs": list(planner.get("evidence_refs") or candidate.get("evidence_refs") or []),
            "reason": str(
                selected.get("decision_reason")
                or (selected.get("critic") or {}).get("reason")
                or "결정론 점수와 위험 보정 결과"
            ),
        },
        "content_design": {
            "topic_family": planner.get("topic_family") or candidate.get("topic_family"),
            "angle": planner.get("angle") or "생활 속 판단 기준",
            "format_type": format_type,
            "emotion_curve": list(planner.get("emotion_curve") or ["공감", "이해", "안심", "행동"]),
            "series_key": planner.get("series_key") or main_keyword,
            "cta_type": "series_next",
        },
        "strategy_source": selected.get("strategy_source", "objective_planner"),
        "planning": dict(selected.get("planning") or {}),
    }


def plan_objective_topic(
    objective_type: str,
    *,
    seed_topic: str | None = None,
    job_id: str | None = None,
    output_path: str | Path | None = None,
    db_path: str | Path | None = None,
    trend_candidates: Sequence[Mapping[str, Any] | str] | None = None,
    planner_call: Callable[[str], Mapping[str, Any]] | None = None,
    critic_call: Callable[[str], Mapping[str, Any]] | None = None,
    allow_ai: bool = True,
) -> dict[str, Any]:
    settings = load_runtime_settings()
    config = build_objective_config(objective_type)
    job_id = job_id or os.environ.get("JOB_ID") or f"auto_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    conn = feedback.connect(Path(db_path) if db_path else None)
    try:
        database_file = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        objective_id = _latest_objective(conn, config)
        observed_date = datetime.now(timezone.utc).date().isoformat()
        for trend in trend_candidates or ():
            if isinstance(trend, Mapping):
                topic = str(trend.get("keyword") or trend.get("topic") or "").strip()
                sources = [str(item) for item in trend.get("sources") or ["suggest"]]
            else:
                topic, sources = str(trend).strip(), ["suggest"]
            for source in sources:
                if topic:
                    conn.execute(
                        "INSERT OR IGNORE INTO trend_observations(topic, source, observed_date) VALUES (?, ?, ?)",
                        (topic, source, observed_date),
                    )
        conn.commit()
        rejected_duplicates: list[dict[str, Any]] = []
        candidates = build_candidate_pool(
            conn, objective_type=config.objective_type, seed_topic=seed_topic,
            trend_candidates=trend_candidates, rejected_duplicates=rejected_duplicates,
        )
        duplicate_threshold = float(feedback.adaptive_topic_thresholds(
            conn, os.environ.get("YOUTUBE_FEEDBACK_STRICTNESS", "balanced")
        )["duplicate"])
        no_unique_candidates = not candidates
        if no_unique_candidates:
            candidates = [{
                "candidate_id": "cand_00",
                "topic": str(seed_topic or "새 주제 후보 없음"),
                "topic_family": _topic_family(str(seed_topic or ""), seed_topic),
                "exploration_mode": "manual", "candidate_source": "none",
                "sources": [], "evidence_refs": [], "source_classification": "insufficient_data",
                "normalized_metrics": {"trend_signal": 0.0, "novelty": 0.0},
                "confidence": 0.0, "duplicate_similarity": 1.0,
                "duplicate_containment": 1.0, "duplicate_threshold": duplicate_threshold,
                "closest_existing_title": (rejected_duplicates[0].get("title") if rejected_duplicates else ""),
                "confounders": ["no_unique_candidate"],
            }]
        cursor = conn.execute(
            """INSERT INTO planning_runs (
                job_id, objective_id, seed_topic, candidate_pool_json,
                selection_mode, decision, created_at
            ) VALUES (?, ?, ?, ?, 'manual', 'manual_review', ?)""",
            (job_id, objective_id, seed_topic, _json(candidates), utc_now()),
        )
        plan_id = int(cursor.lastrowid)
        conn.commit()

        hypotheses = [dict(row) for row in conn.execute(
            "SELECT * FROM strategy_hypotheses WHERE objective_type=? AND status IN ('testing','active','weakened')",
            (config.objective_type,),
        )]
        recent_combinations = {
            (str(row["format_type"] or ""), str(row["hook_type"] or ""))
            for row in conn.execute("""
                SELECT f.format_type, f.hook_type FROM content_features f
                JOIN videos v ON v.video_id=f.video_id
                ORDER BY v.published_at DESC LIMIT 5
            """)
        }
        for candidate in candidates:
            default_item = _default_planner_item(candidate)
            candidate["format_hook_repeat"] = (
                str(default_item.get("format_type") or ""),
                str(default_item.get("hook_type") or ""),
            ) in recent_combinations
        local_rows = _local_candidate_rows(candidates, config.objective_type, job_id)
        preflight_best = float(local_rows[0]["judgment"]["adjusted_score"])
        manual_seed = bool(str(seed_topic or "").strip())
        preflight_threshold = (
            PREFLIGHT_THRESHOLD_MANUAL_SEED if manual_seed else PREFLIGHT_THRESHOLD_AUTO
        )
        preflight_passed = not no_unique_candidates and preflight_best >= preflight_threshold

        # The model only receives the six strongest local candidates. With optimistic
        # local risks, anything below the threshold cannot reach the 55-point runnable
        # threshold for auto-discovery. A manual seed uses a lower gate (see constants
        # above) because the seed itself is the intent signal, not the local score.
        planner_candidates = [item["candidate"] for item in local_rows[:6]]
        planner_refs = valid_evidence_refs(planner_candidates)
        video_ids = [row["video_id"] for row in conn.execute("SELECT video_id FROM videos")]
        titles = _existing_titles(conn)
        planner_error = None
        if not allow_ai and planner_call is None:
            planner_status = "disabled"
            planner_output = {"candidates": [_default_planner_item(item) for item in candidates]}
            strategy_source = "deterministic_fallback"
        elif not preflight_passed:
            planner_status = "skipped"
            planner_output = {"candidates": [_default_planner_item(item) for item in candidates]}
            strategy_source = "local_preflight"
        else:
            try:
                planner_prompt = _planner_prompt(config, planner_candidates, hypotheses)
                if planner_call:
                    raw_planner = planner_call(planner_prompt)
                else:
                    raw_planner = call_claude_json(
                        planner_prompt, model=settings.claude_planner_model,
                        max_tokens=settings.claude_planner_max_tokens,
                        stage="candidate_planner", job_id=job_id, plan_id=plan_id,
                    )
                planner_output = validate_planner_output(
                    raw_planner, planner_candidates, valid_refs=planner_refs,
                    existing_video_ids=video_ids, existing_titles=titles,
                )
                seed_words = _compact_words(seed_topic or "")
                planner_containment_cutoff = (
                    CONTAINMENT_CUTOFF_MANUAL_SEED
                    if str(seed_topic or "").strip()
                    else CONTAINMENT_CUTOFF_DEFAULT
                )
                for item in planner_output["candidates"]:
                    duplicate = _topic_duplicate_info(
                        item["topic"], titles, duplicate_threshold,
                        containment_cutoff=planner_containment_cutoff,
                    )
                    if duplicate["blocked"]:
                        rejected_duplicates.append({"topic": item["topic"], "stage": "planner", **duplicate})
                        raise PlannerValidationError(
                            f"Planner 후보가 기존 제목과 중복됩니다: {duplicate['similarity']:.2f}"
                        )
                    if seed_words and len(seed_words & _compact_words(item["topic"])) < min(2, len(seed_words)):
                        raise PlannerValidationError("Planner가 사용자가 지정한 씨드 범위를 벗어났습니다.")
                planner_status = "success"
                strategy_source = "objective_planner"
            except Exception as exc:
                planner_error = str(exc)
                planner_status = "failed"
                planner_output = {"candidates": [_default_planner_item(item) for item in candidates]}
                strategy_source = "deterministic_fallback"

        planner_map = {item["candidate_id"]: item for item in planner_output["candidates"]}
        initial = []
        for candidate in candidates:
            planner_item = planner_map.get(candidate["candidate_id"], _default_planner_item(candidate))
            candidate["format_hook_repeat"] = (
                str(planner_item.get("format_type") or ""),
                str(planner_item.get("hook_type") or ""),
            ) in recent_combinations
            score = score_candidate(candidate, planner_item, config.objective_type)
            initial.append({"candidate": candidate, "planner": planner_item, "score": score})
        initial.sort(key=lambda item: item["score"]["base_score"], reverse=True)
        top_three = initial[:3]
        critic_error = None
        if planner_status != "success":
            critic_status = "skipped"
            critic_output = {"reviews": []}
        else:
            try:
                critic_prompt = _critic_prompt([
                    {**item["candidate"], **item["planner"], **item["score"]} for item in top_three
                ])
                if critic_call:
                    raw_critic = critic_call(critic_prompt)
                else:
                    raw_critic = call_claude_json(
                        critic_prompt, model=settings.claude_critic_model,
                        max_tokens=settings.claude_critic_max_tokens,
                        stage="candidate_critic", job_id=job_id, plan_id=plan_id,
                    )
                critic_output = validate_critic_output(
                    raw_critic, [item["candidate"]["candidate_id"] for item in top_three],
                    valid_refs=valid_evidence_refs([item["candidate"] for item in top_three]),
                )
                critic_status = "success"
            except Exception as exc:
                critic_error = str(exc)
                critic_status = "failed"
                critic_output = {"reviews": []}
        critic_map = {item["candidate_id"]: item for item in critic_output["reviews"]}

        stale_strategy = bool(conn.execute(
            "SELECT 1 FROM strategy_hypotheses WHERE objective_type=? AND status='expired' LIMIT 1",
            (config.objective_type,),
        ).fetchone())
        desired_mode = exploration_target(job_id)
        judged = []
        for item in initial:
            candidate_id = item["candidate"]["candidate_id"]
            critic = critic_map.get(candidate_id)
            judgment = judge_candidate(
                item["candidate"], item["score"], critic,
                desired_exploration=desired_mode, stale_strategy=stale_strategy,
            )
            judged.append({
                **item, "critic": critic, "judgment": judgment,
                "objective_type": config.objective_type, "strategy_source": strategy_source,
            })
        judged.sort(key=lambda item: item["judgment"]["adjusted_score"], reverse=True)
        eligible = [item for item in judged if item["judgment"]["decision"] != "rejected"]
        selected = eligible[0] if eligible else judged[0]
        pipeline_ready = planner_status == "success" and critic_status == "success"
        final_decision = selected["judgment"]["decision"] if eligible and pipeline_ready else "manual_review"
        selected["judgment"]["decision"] = final_decision

        if no_unique_candidates:
            decision_reason = "기존 영상과의 중복 기준을 통과한 새 후보가 없습니다. Claude는 호출하지 않았습니다."
        elif not allow_ai and planner_call is None:
            decision_reason = "AI 비활성화 실행이므로 자동 제작하지 않고 후보만 저장했습니다."
        elif not preflight_passed:
            decision_reason = (
                f"로컬 사전 평가에서 실행 가능 점수({preflight_threshold:.0f}점 기준, "
                f"{'씨드 완화' if manual_seed else '자동 탐색'} 모드)에 도달할 후보가 없어 "
                "Claude는 호출하지 않았습니다."
            )
        elif planner_status == "failed":
            decision_reason = f"Planner 응답 검증 실패로 Critic을 호출하지 않았습니다: {planner_error}"
        elif critic_status == "failed":
            decision_reason = f"Critic 응답 검증 실패로 자동 제작을 중단했습니다: {critic_error}"
        else:
            decision_reason = str((selected.get("critic") or {}).get("reason") or "점수와 위험 검토 결과")
        selected["decision_reason"] = decision_reason
        selected["planning"] = {
            "preflight_status": "passed" if preflight_passed else "blocked",
            "preflight_best_score": round(preflight_best, 4),
            "preflight_threshold": preflight_threshold,
            "preflight_mode": "manual_seed" if manual_seed else "auto",
            "planner_status": planner_status,
            "critic_status": critic_status,
            "candidate_count": len(candidates) if not no_unique_candidates else 0,
            "planner_candidate_count": len(planner_candidates) if preflight_passed else 0,
            "format_hook_skipped": len(planner_output.get("skipped_candidates") or []),
            "duplicates_rejected": len(rejected_duplicates),
            "duplicate_threshold": duplicate_threshold,
            "claude_cost_usd": round(usage_total(db_path=database_file, job_id=job_id), 8),
        }

        conn.execute(
            """UPDATE planning_runs SET
                planner_output_json=?, critic_output_json=?, selected_candidate_json=?,
                selection_mode=?, base_score=?, adjusted_score=?, confidence=?, decision=?
                WHERE plan_id=?""",
            (
                _json({**planner_output, "error": planner_error}),
                _json({**critic_output, "error": critic_error}),
                _json({**selected["candidate"], **selected["planner"], "judgment": selected["judgment"]}),
                selected["candidate"].get("exploration_mode") or "manual",
                selected["judgment"]["base_score"], selected["judgment"]["adjusted_score"],
                selected["judgment"]["confidence"], final_decision, plan_id,
            ),
        )
        plan = _topic_plan(selected, plan_id, objective_id)
        if final_decision in {"selected", "limited_test"}:
            research_category_id = ((selected.get("candidate") or {}).get("research_category") or {}).get("category_id")
            if research_category_id:
                try:
                    record_category_usage(research_category_id)
                except OSError as exc:
                    print(f"research_category_usage 기록 실패(계속 진행): {exc}", file=sys.stderr)
            statement = (
                f"{plan['content_design'].get('topic_family') or '건강'} 주제의 "
                f"{plan['content_design'].get('format_type') or '설명'} 형식은 "
                f"{objective_label(config.objective_type)} 목표에 유리할 수 있다"
            )
            hypothesis = conn.execute(
                "SELECT hypothesis_id FROM strategy_hypotheses WHERE objective_type=? AND statement=? AND status!='expired'",
                (config.objective_type, statement),
            ).fetchone()
            if hypothesis is None:
                now = utc_now()
                conn.execute("""
                    INSERT INTO strategy_hypotheses (
                        objective_type, statement, evidence_refs_json,
                        contradiction_refs_json, confounders_json, confidence,
                        status, ttl_videos, videos_since_validation, created_at, updated_at
                    ) VALUES (?, ?, ?, '[]', ?, 0.5, 'testing', 10, 0, ?, ?)
                """, (
                    config.objective_type, statement,
                    _json(plan["objective"].get("evidence_refs") or []),
                    _json(["small_sample"] if plan["objective"]["confidence"] < 0.6 else []),
                    now, now,
                ))
            feedback.register_video_job(
                conn, job_id=job_id, topic=plan["topic"], plan_id=plan_id, objective_id=objective_id
            )
        conn.commit()
    finally:
        conn.close()
    if output_path:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return plan


def goal_status(db_path: str | Path | None = None) -> dict[str, Any]:
    conn = feedback.connect(Path(db_path) if db_path else None)
    try:
        row = conn.execute("""
            SELECT p.*, o.objective_type FROM planning_runs p
            JOIN objectives o ON o.objective_id=p.objective_id
            ORDER BY p.plan_id DESC LIMIT 1
        """).fetchone()
        if row is None:
            return {"available": False}
        return {
            "available": True, "plan_id": row["plan_id"], "job_id": row["job_id"],
            "objective_type": row["objective_type"], "decision": row["decision"],
            "base_score": row["base_score"], "adjusted_score": row["adjusted_score"],
            "confidence": row["confidence"],
            "selected": json.loads(row["selected_candidate_json"] or "{}"),
            "created_at": row["created_at"],
        }
    finally:
        conn.close()


def goal_report(db_path: str | Path | None = None, limit: int = 15) -> dict[str, Any]:
    conn = feedback.connect(Path(db_path) if db_path else None)
    try:
        rows = conn.execute("""
            SELECT p.*, o.objective_type FROM planning_runs p
            JOIN objectives o ON o.objective_id=p.objective_id
            ORDER BY p.plan_id DESC LIMIT ?
        """, (int(limit),)).fetchall()
        hypotheses = [dict(row) for row in conn.execute(
            "SELECT * FROM strategy_hypotheses ORDER BY updated_at DESC"
        )]
        completed = conn.execute("SELECT COUNT(*) FROM video_jobs WHERE video_id IS NOT NULL").fetchone()[0]
        return {
            "generated_at": utc_now(), "completed_goal_videos": int(completed),
            "refresh_due": {
                "quick_review": completed > 0 and completed % 5 == 0,
                "full_audit": completed > 0 and completed % 15 == 0,
                "baseline_rebuild": completed > 0 and completed % 30 == 0,
            },
            "runs": [dict(row) for row in rows], "hypotheses": hypotheses,
        }
    finally:
        conn.close()


def clamp_weight_change(current: float, proposed: float) -> float:
    lower, upper = current * 0.90, current * 1.10
    return min(upper, max(lower, proposed))


def _review_due(conn: sqlite3.Connection, objective_type: str) -> tuple[str | None, int]:
    count = int(conn.execute("""
        SELECT COUNT(*) FROM video_jobs j JOIN objectives o ON o.objective_id=j.objective_id
        WHERE o.objective_type=? AND j.video_id IS NOT NULL
    """, (objective_type,)).fetchone()[0])
    if count <= 0:
        return None, count
    if count % 30 == 0:
        audit_type = "baseline_rebuild"
    elif count % 15 == 0:
        audit_type = "full_audit"
    elif count % 5 == 0:
        audit_type = "quick_review"
    else:
        return None, count
    existing = conn.execute(
        "SELECT 1 FROM strategy_audits WHERE objective_type=? AND audit_type=? AND video_count=?",
        (objective_type, audit_type, count),
    ).fetchone()
    return (None if existing else audit_type), count


def _needs_sonnet_audit(conn: sqlite3.Connection, objective_type: str) -> bool:
    rows = conn.execute("""
        SELECT p.adjusted_score FROM planning_runs p
        JOIN objectives o ON o.objective_id=p.objective_id
        WHERE o.objective_type=? AND p.adjusted_score IS NOT NULL
        ORDER BY p.plan_id DESC LIMIT 4
    """, (objective_type,)).fetchall()
    chronological = [float(row["adjusted_score"]) for row in reversed(rows)]
    three_declines = len(chronological) >= 4 and all(
        chronological[index] > chronological[index + 1]
        for index in range(len(chronological) - 1)
    )
    failed_hypothesis = bool(conn.execute("""
        SELECT 1 FROM strategy_hypotheses
        WHERE objective_type=? AND status IN ('weakened','rejected')
          AND contradiction_count>=3 LIMIT 1
    """, (objective_type,)).fetchone())
    recent = conn.execute("""
        SELECT p.critic_output_json, p.decision FROM planning_runs p
        JOIN objectives o ON o.objective_id=p.objective_id
        WHERE o.objective_type=? ORDER BY p.plan_id DESC LIMIT 5
    """, (objective_type,)).fetchall()
    critic_conflicts = 0
    for row in recent:
        try:
            reviews = json.loads(row["critic_output_json"] or "{}").get("reviews") or []
        except json.JSONDecodeError:
            reviews = []
        if row["decision"] in {"selected", "limited_test"} and any(
            item.get("recommended_action") == "rejected" for item in reviews if isinstance(item, Mapping)
        ):
            critic_conflicts += 1
    return three_declines or failed_hypothesis or critic_conflicts >= 2


def run_due_strategy_review(
    objective_type: str,
    *,
    job_id: str | None = None,
    db_path: str | Path | None = None,
    review_call: Callable[[str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    objective_type = normalize_objective_type(objective_type)
    settings = load_runtime_settings()
    conn = feedback.connect(Path(db_path) if db_path else None)
    try:
        audit_type, video_count = _review_due(conn, objective_type)
        if audit_type is None:
            return {"ran": False, "video_count": video_count, "reason": "not_due_or_already_recorded"}
        hypotheses = [dict(row) for row in conn.execute(
            "SELECT * FROM strategy_hypotheses WHERE objective_type=? ORDER BY updated_at DESC",
            (objective_type,),
        )]
        recent = [dict(row) for row in conn.execute("""
            SELECT p.plan_id, p.base_score, p.adjusted_score, p.confidence, p.decision,
                   p.selected_candidate_json
            FROM planning_runs p JOIN objectives o ON o.objective_id=p.objective_id
            WHERE o.objective_type=? ORDER BY p.plan_id DESC LIMIT 15
        """, (objective_type,)).fetchall()]
        if audit_type == "baseline_rebuild":
            # Cohort medians/percentiles are calculated on read, so rebuilding
            # means expiring stale beliefs before the following audit.
            conn.execute("""
                UPDATE strategy_hypotheses SET status='expired', updated_at=?
                WHERE objective_type=? AND videos_since_validation>=ttl_videos
            """, (utc_now(), objective_type))
            baseline = feedback.rebuild_objective_baselines(conn)
        else:
            baseline = None
        full = audit_type in {"full_audit", "baseline_rebuild"}
        model = settings.claude_audit_model
        if full and _needs_sonnet_audit(conn, objective_type):
            model = os.environ.get("CLAUDE_AUDIT_ESCALATION_MODEL", "claude-sonnet-4-6")
        prompt = f"""YouTube Shorts 전략 {audit_type}를 수행하세요.
목표: {objective_type}
최근 실행: {_json(recent)}
전략 가설: {_json(hypotheses)}
가설을 지지하는 내용뿐 아니라 반증, 노출 편향, 길이, 계절성, 작은 표본을 검토하세요.
수치 confidence나 최종 가중치는 만들지 마세요. 가중치 변경은 제안만 하세요.
JSON 필드: current_beliefs, supporting_evidence, contradicting_evidence,
confounders, expired_hypotheses, weight_change_proposals,
reset_recommendation(none|partial|full)."""
        try:
            if review_call:
                output = dict(review_call(prompt))
            else:
                output = call_claude_json(
                    prompt, model=model, max_tokens=settings.claude_audit_max_tokens,
                    stage=f"strategy_{audit_type}", job_id=job_id or f"audit_{video_count}",
                )
        except Exception as exc:
            output = {
                "current_beliefs": [item["statement"] for item in hypotheses if item["status"] == "active"],
                "supporting_evidence": [], "contradicting_evidence": [],
                "confounders": ["review_call_failed"],
                "expired_hypotheses": [item["statement"] for item in hypotheses if item["status"] == "expired"],
                "weight_change_proposals": [], "reset_recommendation": "none",
                "error": str(exc)[:500],
            }
        required = {
            "current_beliefs", "supporting_evidence", "contradicting_evidence", "confounders",
            "expired_hypotheses", "weight_change_proposals", "reset_recommendation",
        }
        if not required.issubset(output) or output.get("reset_recommendation") not in {"none", "partial", "full"}:
            raise PlannerValidationError("전략 감사 JSON schema가 올바르지 않습니다.")
        profile = get_objective_profile(objective_type)
        clamped = []
        for proposal in output.get("weight_change_proposals") or []:
            if not isinstance(proposal, Mapping):
                continue
            metric = str(proposal.get("metric") or "")
            if metric not in profile:
                continue
            try:
                proposed = float(proposal.get("proposed_weight"))
            except (TypeError, ValueError):
                continue
            clamped.append({
                **dict(proposal), "current_weight": profile[metric],
                "clamped_weight": clamp_weight_change(profile[metric], proposed),
            })
        output["weight_change_proposals"] = clamped
        if baseline is not None:
            output["baseline_rebuild"] = baseline
        conn.execute(
            "INSERT INTO strategy_audits (objective_type, audit_type, video_count, model, output_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (objective_type, audit_type, video_count, model, _json(output), utc_now()),
        )
        conn.commit()
        return {"ran": True, "audit_type": audit_type, "video_count": video_count, "model": model, "output": output}
    finally:
        conn.close()
