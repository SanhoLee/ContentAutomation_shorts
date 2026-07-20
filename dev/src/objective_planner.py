"""Goal-driven candidate building, Claude critique, and deterministic judging."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from claude_cost import assert_budget, record_usage
from content_objectives import (
    ObjectiveConfig,
    build_objective_config,
    get_objective_profile,
    normalize_objective_type,
    objective_label,
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
    return [str(row["title"]) for row in conn.execute("SELECT title FROM videos")]


def _candidate_topic(base: str, mode: str, seed: str | None, index: int) -> str:
    base = " ".join(str(base or "").split()).strip()
    if seed and seed not in base:
        base = f"{seed}: {base}"
    suffixes = {
        "exploit": ("놓치기 쉬운 신호", "실천 기준", "오해와 사실", "자가 점검", "작은 습관"),
        "adjacent": ("함께 살펴볼 생활 습관", "의외의 연결", "비교해서 보는 기준"),
        "wildcard": ("하루 한 가지 도전",),
    }
    suffix = suffixes[mode][index % len(suffixes[mode])]
    return f"{base} - {suffix}" if base else suffix


def _mode_targets() -> list[str]:
    return ["exploit"] * 5 + ["adjacent"] * 3 + ["adjacent"] * 3 + ["wildcard"]


def build_candidate_pool(
    conn: sqlite3.Connection,
    *,
    objective_type: str,
    seed_topic: str | None = None,
    trend_candidates: Sequence[Mapping[str, Any] | str] | None = None,
    candidate_count: int = 12,
) -> list[dict[str, Any]]:
    """Build a 5/3/3/1 evidence, adjacent, trend, wildcard candidate pool."""
    objective_type = normalize_objective_type(objective_type)
    rows = _preferred_snapshot_rows(conn)
    existing_titles = _existing_titles(conn)
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

    source_bases = [str(item[1]["title"]) for item in evidence]
    base_fallback = [seed_topic] if seed_topic else []
    base_fallback += list(DEFAULT_TOPICS)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    modes = _mode_targets()
    for index in range(max(candidate_count * 4, 24)):
        if len(candidates) >= candidate_count:
            break
        slot = len(candidates)
        mode = modes[slot] if slot < len(modes) else "wildcard"
        trend_slot = 8 <= slot <= 10
        evidence_item = evidence[index % len(evidence)] if evidence else None
        if trend_slot:
            if trend_values:
                trend = trend_values[(slot - 8) % len(trend_values)]
                topic = trend["topic"]
                sources = trend["sources"]
                repeat_days = int(trend.get("repeat_days") or 0)
            else:
                pool = base_fallback or list(DEFAULT_TOPICS)
                base = pool[index % len(pool)]
                topic = _candidate_topic(base, "adjacent", seed_topic, index)
                sources = ["trend_fallback"]
                repeat_days = 0
            mode = "adjacent"
        else:
            pool = source_bases or base_fallback
            base = pool[index % len(pool)] if pool else DEFAULT_TOPICS[index % len(DEFAULT_TOPICS)]
            topic = _candidate_topic(base, mode, seed_topic, index)
            sources = ["channel_performance"] if evidence_item else ["exploration"]
            repeat_days = 0
        key = re.sub(r"\s+", "", topic).lower()
        if not key or key in seen or any(topic.strip() == title.strip() for title in existing_titles):
            continue
        seen.add(key)
        closest = max((_jaccard_topic(topic, title) for title in existing_titles), default=0.0)
        if evidence_item:
            _, source_row, normalized = evidence_item
            metrics = dict(normalized.get("metrics") or {})
            evidence_refs = [
                f"video:{source_row['video_id']}",
                f"metric:{source_row['window_name']}:{source_row['video_id']}",
            ]
            confidence = float(normalized.get("confidence") or 0.0)
            classification = normalized.get("classification")
            family = source_row["topic_family"] or next(iter(_compact_words(topic)), "기타")
            confounders = []
            if classification == "exposure_luck":
                confounders.append("shorts_feed_exposure")
            if normalized.get("confidence_level") == "low":
                confounders.append("small_sample")
            if source_row["upload_jst_hour"] is not None:
                confounders.append("upload_time_observed_not_causal")
        else:
            metrics, evidence_refs, confidence, classification = {}, [], 0.0, "insufficient_data"
            family = next(iter(_compact_words(topic)), "기타")
            confounders = ["small_sample"]
        source_count = max(1, len(set(sources)))
        metrics.update({
            "trend_signal": min(1.0, 0.30 + 0.18 * source_count + 0.04 * min(repeat_days, 5)) if trend_slot else 0.5,
            "novelty": max(0.0, 1.0 - closest),
        })
        candidates.append({
            "candidate_id": f"cand_{len(candidates) + 1:02d}",
            "topic": topic,
            "topic_family": family,
            "exploration_mode": mode,
            "candidate_source": (
                "performance_exploit" if slot < 5 else "adjacent" if slot < 8
                else "trend" if slot < 11 else "wildcard"
            ),
            "sources": sources,
            "evidence_refs": evidence_refs,
            "source_classification": classification,
            "normalized_metrics": metrics,
            "confidence": confidence,
            "duplicate_similarity": closest,
            "confounders": confounders,
        })
    return candidates


def valid_evidence_refs(conn: sqlite3.Connection, candidates: Sequence[Mapping[str, Any]]) -> set[str]:
    refs = {str(ref) for candidate in candidates for ref in candidate.get("evidence_refs", [])}
    for row in conn.execute("SELECT video_id, window_name FROM performance_snapshots"):
        refs.add(f"video:{row['video_id']}")
        refs.add(f"metric:{row['window_name']}:{row['video_id']}")
    return refs


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
    allowed_refs = set(valid_refs or ())
    allowed_videos = set(existing_video_ids or ())
    titles = {str(title).strip().lower() for title in (existing_titles or ())}
    sanitized = []
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
            if allowed_refs and ref not in allowed_refs:
                raise PlannerValidationError(f"입력 데이터에 없는 evidence_ref입니다: {ref}")
            if ref.startswith("video:") and allowed_videos and ref.split(":", 1)[1] not in allowed_videos:
                raise PlannerValidationError(f"DB에 없는 video_id입니다: {ref}")
        for field in ENUM_FIELDS:
            if field in raw and str(raw[field]) not in ENUM_SCORE:
                raise PlannerValidationError(f"허용되지 않은 enum입니다: {field}={raw[field]}")
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
        if item.get("format_type") and item["format_type"] not in FORMAT_TYPES:
            raise PlannerValidationError(f"허용되지 않은 format_type입니다: {item['format_type']}")
        sanitized.append(item)
    if not sanitized:
        raise PlannerValidationError("Planner가 유효 후보를 반환하지 않았습니다.")
    return {"candidates": sanitized}


def validate_critic_output(
    output: Mapping[str, Any],
    candidate_ids: Iterable[str],
    *,
    valid_refs: Iterable[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(output, Mapping) or not isinstance(output.get("reviews"), list):
        raise PlannerValidationError("Critic JSON schema가 올바르지 않습니다.")
    allowed_ids = set(candidate_ids)
    allowed_refs = set(valid_refs or ())
    reviews = []
    for raw in output["reviews"]:
        candidate_id = str(raw.get("candidate_id") or "")
        if candidate_id not in allowed_ids:
            raise PlannerValidationError(f"Critic이 알 수 없는 후보를 참조했습니다: {candidate_id}")
        for field in CRITIC_ENUM_FIELDS:
            if str(raw.get(field) or "medium") not in ENUM_SCORE:
                raise PlannerValidationError(f"허용되지 않은 Critic enum입니다: {field}")
        refs = [str(ref) for ref in raw.get("contradicting_refs") or []]
        if allowed_refs and any(ref not in allowed_refs for ref in refs):
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
    return f"""목표 기반 YouTube Shorts 후보를 정성적으로 설계하세요.
목표: {config.objective_type} ({objective_label(config.objective_type)})
목표 가중치(코드 계산용이며 재계산 금지): {_json(config.weights)}
후보 데이터: {_json(candidates)}
활성 전략 가설: {_json(hypotheses)}

규칙:
- 숫자 점수, 검색량, 성과 수치, 인과관계를 새로 만들지 마세요.
- candidate_id와 evidence_refs는 입력에 있는 값만 사용하세요.
- 같은 제목을 복사하지 말고 각도와 형식을 구체화하세요.
- 정성 enum은 low/medium/high만 사용하세요.
- candidates 배열을 가진 JSON 객체만 출력하세요.
각 후보 필드: candidate_id, topic, topic_family, angle, format_type, hook_type,
emotion_curve, series_key, series_potential, channel_fit, family_relevance,
actionability, narrative_fit, topic_trust, evidence_refs, risk_flags."""


def _critic_prompt(candidates: Sequence[Mapping[str, Any]]) -> str:
    return f"""아래 상위 Shorts 후보가 틀릴 수 있는 이유를 찾으세요. 후보를 지지하지 마세요.
조회수 우연, Shorts Feed 노출 편향, 업로드 시간, 영상 길이, 계절성, 작은 표본,
주제 중복, 제목-내용 불일치, 채널 과적합을 검토하세요.
후보: {_json(candidates)}
숫자 점수를 만들지 말고 reviews 배열을 가진 JSON만 출력하세요.
각 review 필드: candidate_id, contradicting_refs, confounders, duplicate_risk,
overfit_risk, evidence_risk, recommended_action, reason.
위험 enum은 low/medium/high, recommended_action은 selected/limited_test/rejected 중 하나입니다."""


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
    record_usage(
        stage, model, data, jsonl_path=usage_path, job_id=job_id, plan_id=plan_id
    )
    text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
    text = re.sub(r"^```(?:json)?", "", text.strip()).rstrip("`").strip()
    return json.loads(text)


def _qualitative_score(planner: Mapping[str, Any]) -> float:
    values = [ENUM_SCORE[str(planner[field])] for field in ENUM_FIELDS if field in planner]
    return statistics_mean(values, default=0.5)


def statistics_mean(values: Sequence[float], default: float = 0.5) -> float:
    return sum(values) / len(values) if values else default


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
    base_score = 100.0 * (
        metric_score * 0.70 + qualitative_score * 0.20 + trend_novelty * 0.10
    )
    return {
        "metric_score": round(metric_score, 6),
        "qualitative_score": round(qualitative_score, 6),
        "trend_novelty_score": round(trend_novelty, 6),
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
    elif adjusted >= 55.0 or confidence < 0.6:
        decision = "limited_test"
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
            "evidence_refs": list(planner.get("evidence_refs") or candidate.get("evidence_refs") or []),
            "reason": str((selected.get("critic") or {}).get("reason") or "결정론 점수와 위험 보정 결과"),
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
        candidates = build_candidate_pool(
            conn, objective_type=config.objective_type, seed_topic=seed_topic,
            trend_candidates=trend_candidates,
        )
        if not candidates:
            raise RuntimeError("기획 후보를 만들지 못했습니다.")
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
        refs = valid_evidence_refs(conn, candidates)
        video_ids = [row["video_id"] for row in conn.execute("SELECT video_id FROM videos")]
        titles = _existing_titles(conn)
        planner_prompt = _planner_prompt(config, candidates, hypotheses)
        planner_error = None
        try:
            if planner_call:
                raw_planner = planner_call(planner_prompt)
            elif allow_ai:
                raw_planner = call_claude_json(
                    planner_prompt, model=settings.claude_planner_model,
                    max_tokens=settings.claude_planner_max_tokens,
                    stage="candidate_planner", job_id=job_id, plan_id=plan_id,
                )
            else:
                raise RuntimeError("AI disabled")
            planner_output = validate_planner_output(
                raw_planner, candidates, valid_refs=refs,
                existing_video_ids=video_ids, existing_titles=titles,
            )
            strategy_source = "objective_planner"
        except Exception as exc:
            planner_error = str(exc)
            planner_output = {"candidates": [_default_planner_item(item) for item in candidates]}
            strategy_source = "deterministic_fallback"

        planner_map = {item["candidate_id"]: item for item in planner_output["candidates"]}
        recent_combinations = {
            (str(row["format_type"] or ""), str(row["hook_type"] or ""))
            for row in conn.execute("""
                SELECT f.format_type, f.hook_type FROM content_features f
                JOIN videos v ON v.video_id=f.video_id
                ORDER BY v.published_at DESC LIMIT 5
            """)
        }
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
        try:
            critic_prompt = _critic_prompt([
                {**item["candidate"], **item["planner"], **item["score"]} for item in top_three
            ])
            if critic_call:
                raw_critic = critic_call(critic_prompt)
            elif allow_ai:
                raw_critic = call_claude_json(
                    critic_prompt, model=settings.claude_critic_model,
                    max_tokens=settings.claude_critic_max_tokens,
                    stage="candidate_critic", job_id=job_id, plan_id=plan_id,
                )
            else:
                raise RuntimeError("AI disabled")
            critic_output = validate_critic_output(
                raw_critic, [item["candidate"]["candidate_id"] for item in top_three],
                valid_refs=refs,
            )
        except Exception as exc:
            critic_error = str(exc)
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
            if strategy_source == "deterministic_fallback":
                judgment["decision"] = "limited_test" if judgment["decision"] != "rejected" else "rejected"
            judged.append({
                **item, "critic": critic, "judgment": judgment,
                "objective_type": config.objective_type, "strategy_source": strategy_source,
            })
        judged.sort(key=lambda item: item["judgment"]["adjusted_score"], reverse=True)
        eligible = [item for item in judged if item["judgment"]["decision"] != "rejected"]
        selected = eligible[0] if eligible else judged[0]
        final_decision = selected["judgment"]["decision"] if eligible else "manual_review"
        selected["judgment"]["decision"] = final_decision

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
