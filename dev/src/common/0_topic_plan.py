#!/usr/bin/env python3
"""CLI entry point for objective-driven topic planning."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from content_objectives import normalize_objective_type, objective_label
from objective_planner import (
    feedback, goal_report, goal_status, plan_objective_topic, run_due_strategy_review,
)


def _request_json(url: str, params: dict[str, str]) -> Any:
    request = Request(f"{url}?{urlencode(params)}", headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(request, timeout=8) as response:
        text = response.read().decode("utf-8", errors="replace").strip()
    if text.startswith(")]}'"):
        text = text.split("\n", 1)[1]
    return json.loads(text)


def collect_trend_signals(seed: str | None) -> list[dict[str, Any]]:
    if not seed:
        return []
    found: dict[str, set[str]] = {}
    calls = (
        ("google_suggest", "https://suggestqueries.google.com/complete/search",
         {"client": "firefox", "hl": "ko", "gl": "KR", "q": seed}),
        ("youtube_suggest", "https://suggestqueries.google.com/complete/search",
         {"client": "firefox", "ds": "yt", "hl": "ko", "gl": "KR", "q": seed}),
    )
    for source, url, params in calls:
        try:
            payload = _request_json(url, params)
            values = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
        except Exception as exc:
            print(f"트렌드 소스 {source} 조회 실패(기존 데이터로 계속): {type(exc).__name__}", file=sys.stderr)
            values = []
        for value in values:
            topic = " ".join(str(value).split()).strip()
            if topic:
                found.setdefault(topic, set()).add(source)
    try:
        payload = _request_json(
            f"https://trends.google.com/trends/api/autocomplete/{quote(seed)}",
            {"hl": "ko", "tz": "-540"},
        )
        for value in (payload.get("default") or {}).get("topics") or []:
            topic = " ".join(str(value.get("title") or "").split()).strip()
            if topic:
                found.setdefault(topic, set()).add("google_trends_autocomplete")
    except Exception as exc:
        print(f"트렌드 소스 google_trends 조회 실패(기존 데이터로 계속): {type(exc).__name__}", file=sys.stderr)
    return [
        {"keyword": topic, "sources": sorted(sources)}
        for topic, sources in sorted(found.items(), key=lambda item: (-len(item[1]), item[0]))
    ][:20]


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
    if args.require_runnable and objective["decision"] in {"manual_review", "rejected"}:
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
