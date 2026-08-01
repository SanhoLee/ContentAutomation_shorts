#!/usr/bin/env python3
"""CLI entry point for objective-driven topic planning."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

import trend_probe
from content_objectives import normalize_objective_type, objective_label
from objective_planner import (
    feedback, goal_report, goal_status, plan_objective_topic, run_due_strategy_review,
)


def collect_trend_signals(seed: str | None) -> list[dict[str, Any]]:
    """Autocomplete demand for a seed, filtered to what this channel is about.

    Delegates to trend_probe, which walks en → ja → ko and stops at the first
    language that stays on topic. The old version asked Korean only, without
    `oe=utf-8` (so results came back mojibake) and without any relevance check —
    which is how "꿈돌이" and "꿈빛파티시엘" ended up in trend_observations.
    """
    if not seed:
        return []
    conn = None
    try:
        conn = feedback.connect()
        vocabulary = trend_probe.build_channel_vocabulary(conn)
    except Exception as exc:
        print(f"채널 어휘 로드 실패(필터 없이 계속): {type(exc).__name__}", file=sys.stderr)
        vocabulary = trend_probe.build_channel_vocabulary(None)
    finally:
        if conn is not None:
            conn.close()

    result = trend_probe.probe(seed, vocabulary)
    if not result.ok:
        print(
            f"트렌드 신호 없음: {result.note} "
            f"(시도: {[a.get('language') for a in result.attempts]})",
            file=sys.stderr,
        )
        return []
    print(
        f"트렌드 {len(result.keywords)}건 수집 (locale={result.language}, "
        f"주제적합 {result.domain_match:.0%}, 상업성 {result.dropped_commercial}건 제외)"
    )
    source = f"suggest_{result.language}"
    return [{"keyword": keyword, "sources": [source]} for keyword in result.keywords][:20]


def maybe_sync(no_sync: bool) -> str:
    conn = feedback.connect()
    try:
        status = feedback.feedback_cache_status(
            conn, ttl_hours=int(os.environ.get("YOUTUBE_FEEDBACK_SYNC_TTL_HOURS", "6"))
        )
    finally:
        conn.close()
    if no_sync or status == "fresh-cache":
        return status
    token = os.environ.get("YOUTUBE_FEEDBACK_TOKEN")
    if token and Path(token).expanduser().is_file():
        if feedback.cmd_sync(argparse.Namespace()) == 0:
            return "success"
        conn = feedback.connect()
        try:
            after_failure = feedback.feedback_cache_status(
                conn, ttl_hours=int(os.environ.get("YOUTUBE_FEEDBACK_SYNC_TTL_HOURS", "6"))
            )
        finally:
            conn.close()
        return "stale-cache" if after_failure in {"stale-cache", "missing"} else "refresh-failed-cache"
    return status


def cmd_plan(args: argparse.Namespace) -> int:
    sync_status = maybe_sync(args.no_sync)
    if sync_status in {"stale-cache", "missing"} and not args.allow_stale:
        objective_type = normalize_objective_type(args.objective)
        reason = "정상 YouTube 동기화 데이터가 7일 이내가 아니어서 Claude 호출 전에 중단했습니다."
        conn = feedback.connect()
        try:
            sync_error = feedback.latest_sync_error(conn)
        finally:
            conn.close()
        if sync_error:
            reason += f" 최근 동기화 실패 원인: {sync_error}"
        plan = {
            "topic": args.seed or "",
            "main_keyword": args.seed or "",
            "title": args.seed or "",
            "objective": {
                "type": objective_type, "objective_id": None, "plan_id": None,
                "selection_mode": "manual", "confidence": 0.0,
                "decision": "manual_review", "base_score": 0.0, "adjusted_score": 0.0,
                "evidence_refs": [], "reason": reason, "sync_status": sync_status,
            },
            "content_design": {},
            "strategy_source": "sync_preflight",
            "planning": {
                "preflight_status": "blocked_stale_data",
                "planner_status": "skipped", "critic_status": "skipped",
                "candidate_count": 0, "duplicates_rejected": 0,
                "claude_cost_usd": 0.0,
            },
        }
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"목표: {objective_label(objective_type)}")
        print("상태: manual_review")
        print(f"주의: {reason}")
        print(f"topic_plan.json: {target}")
        return 2 if args.require_runnable else 0

    trends = [] if args.no_trends else collect_trend_signals(args.seed)
    plan = plan_objective_topic(
        args.objective, seed_topic=args.seed, job_id=args.job_id,
        output_path=args.output, trend_candidates=trends, allow_ai=not args.no_ai,
    )
    plan["objective"]["sync_status"] = sync_status
    if sync_status == "refresh-failed-cache":
        conn = feedback.connect()
        try:
            sync_error = feedback.latest_sync_error(conn)
        finally:
            conn.close()
        if sync_error:
            existing_reason = plan["objective"].get("reason") or ""
            plan["objective"]["reason"] = (
                f"{existing_reason} (성과 데이터 동기화 실패: {sync_error}; 이전 캐시로 계속 진행)"
            ).strip()
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    objective = plan["objective"]
    print(f"목표: {objective_label(objective['type'])}")
    print(f"상태: {objective['decision']}")
    print(f"선정 주제: {plan['topic']}")
    print(f"근거: {', '.join(objective.get('evidence_refs') or []) or '초기 탐색 후보'}")
    print(f"주의: {objective.get('reason', '')}")
    print(f"topic_plan.json: {target}")
    # manual_review/rejected still carries a real topic candidate (deterministic
    # fallback always produces one) and production should proceed regardless —
    # quality gating happens later via YouTube feedback, not by blocking here.
    # The only genuine "cannot continue" case is zero candidates at all.
    planning = plan.get("planning") or {}
    if args.require_runnable and int(planning.get("candidate_count") or 0) == 0:
        return 2
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    data = goal_status()
    if not data.get("available"):
        print("목표 기획 이력이 없습니다.")
        return 0
    selected = data.get("selected") or {}
    print(f"목표: {objective_label(data['objective_type'])}")
    print(f"상태: {data['decision']}")
    print(f"주제: {selected.get('topic', '-')}")
    print(f"조정 점수: {data.get('adjusted_score')} / 확신도: {data.get('confidence')}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    data = goal_report(limit=args.limit)
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.output:
        target = Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text + "\n", encoding="utf-8")
        print(target)
    else:
        print(text)
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    result = run_due_strategy_review(args.objective, job_id=args.job_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="목표 지표 기반 Shorts 기획")
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan", help="후보 생성·Planner·Critic·Judge 실행")
    plan.add_argument("--objective", required=True, help="subscriber_growth/reach/retention/share_growth/balanced 또는 한국어 별칭")
    plan.add_argument("--seed")
    plan.add_argument("--job-id", default=os.environ.get("JOB_ID"))
    plan.add_argument("--output", default=str(Path(os.environ.get("WORK_DIR", ".")) / "topic_plan.json"))
    plan.add_argument("--no-ai", action="store_true", help="Planner/Critic 없이 deterministic dry-run")
    plan.add_argument("--no-sync", action="store_true")
    plan.add_argument("--no-trends", action="store_true")
    plan.add_argument("--allow-stale", action="store_true")
    plan.add_argument("--require-runnable", action="store_true")
    status = subparsers.add_parser("status", help="최근 목표 기획 상태")
    report = subparsers.add_parser("report", help="목표 기획·가설 보고서")
    report.add_argument("--limit", type=int, default=15)
    report.add_argument("--output")
    refresh = subparsers.add_parser("refresh", help="5/15/30편 전략 리프레시 실행")
    refresh.add_argument("--objective", required=True)
    refresh.add_argument("--job-id", default=os.environ.get("JOB_ID"))
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return {
        "plan": cmd_plan, "status": cmd_status, "report": cmd_report,
        "refresh": cmd_refresh,
    }[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
