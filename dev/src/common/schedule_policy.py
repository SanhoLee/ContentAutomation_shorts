#!/usr/bin/env python3
"""schedule_policy.py — "언제, 몇 개까지" from dev/config/schedules.yaml (Phase 5).

This module runs nothing itself. An auto entry point (run_goal.sh today, an
eventual cron/systemd timer later) calls can_run() before starting a job and
record_publish() after a successful publish; everything else here is a pure
read of the YAML config plus a small append-only JSONL log.

`yaml` is already a hard runtime dependency of this repo -- config.sh parses
config.yaml with it on every pipeline invocation -- so this reuses that
instead of adding a new dependency for one small file.
"""

from __future__ import annotations

import argparse
import json
from datetime import date as date_cls, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

COMMON_DIR = Path(__file__).resolve().parent
DEV_DIR = COMMON_DIR.parents[1]
SCHEDULES_PATH = DEV_DIR / "config" / "schedules.yaml"
PUBLISH_LOG_PATH = DEV_DIR / "data" / "ops" / "publish_log.jsonl"

DEFAULT_SCHEDULE: dict[str, Any] = {
    "timezone": "Asia/Seoul",
    "platforms": {},
    "max_jobs_per_day": 0,  # 0 = no global cap
}


def load_schedule(path: str | Path | None = None) -> dict[str, Any]:
    """Read schedules.yaml, degrading to an all-disabled default on any
    missing/invalid file so a broken config parks jobs rather than running
    them unbounded."""
    target = Path(path) if path else SCHEDULES_PATH
    try:
        raw = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        raw = {}
    merged = {**DEFAULT_SCHEDULE, **raw}
    merged["platforms"] = raw.get("platforms") or {}
    return merged


def _platform_config(schedule: dict[str, Any], platform: str) -> dict[str, Any]:
    return schedule.get("platforms", {}).get(platform) or {}


def _tz(schedule: dict[str, Any]) -> ZoneInfo:
    try:
        return ZoneInfo(schedule.get("timezone") or "UTC")
    except Exception:
        return ZoneInfo("UTC")


def _now(schedule: dict[str, Any], now: datetime | None = None) -> datetime:
    if now is not None:
        return now if now.tzinfo else now.replace(tzinfo=_tz(schedule))
    return datetime.now(_tz(schedule))


def _read_publish_log(log_path: str | Path | None = None) -> list[dict[str, Any]]:
    target = Path(log_path) if log_path else PUBLISH_LOG_PATH
    if not target.exists():
        return []
    records = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def remaining_quota(
    platform: str, day: date_cls | None = None, *,
    schedule: dict[str, Any] | None = None, log_path: str | Path | None = None,
) -> int:
    schedule = schedule or load_schedule()
    limit = int(_platform_config(schedule, platform).get("daily_limit", 0) or 0)
    day = day or _now(schedule).date()
    day_str = day.isoformat()
    used = sum(
        1 for r in _read_publish_log(log_path)
        if r.get("platform") == platform and str(r.get("date", "")) == day_str
    )
    return max(0, limit - used)


def can_run(
    platform: str, now: datetime | None = None, *,
    schedule: dict[str, Any] | None = None, log_path: str | Path | None = None,
) -> bool:
    """Whether `platform` is allowed to run right now: enabled, under its own
    daily_limit, and under the global max_jobs_per_day (0 = uncapped)."""
    schedule = schedule or load_schedule()
    cfg = _platform_config(schedule, platform)
    if not cfg.get("enabled", False):
        return False
    now = _now(schedule, now)
    if remaining_quota(platform, now.date(), schedule=schedule, log_path=log_path) <= 0:
        return False
    global_limit = int(schedule.get("max_jobs_per_day", 0) or 0)
    if global_limit > 0:
        day_str = now.date().isoformat()
        total_today = sum(1 for r in _read_publish_log(log_path) if str(r.get("date", "")) == day_str)
        if total_today >= global_limit:
            return False
    return True


def next_slot(
    platform: str, *, schedule: dict[str, Any] | None = None, now: datetime | None = None,
) -> datetime | None:
    """The next configured time-of-day slot for `platform`, today or tomorrow."""
    schedule = schedule or load_schedule()
    times = _platform_config(schedule, platform).get("times") or []
    now = _now(schedule, now)
    candidates = []
    for value in times:
        try:
            hour, minute = (int(part) for part in str(value).split(":"))
        except ValueError:
            continue
        candidates.append(now.replace(hour=hour, minute=minute, second=0, microsecond=0))
    if not candidates:
        return None
    future = [c for c in candidates if c > now]
    if future:
        return min(future)
    return min(candidates) + timedelta(days=1)


def record_publish(
    platform: str, job_id: str, *,
    log_path: str | Path | None = None, now: datetime | None = None, schedule: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schedule = schedule or load_schedule()
    now = _now(schedule, now)
    target = Path(log_path) if log_path else PUBLISH_LOG_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "platform": platform, "job_id": job_id,
        "date": now.date().isoformat(), "published_at": now.isoformat(timespec="seconds"),
    }
    with open(target, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


# ---------------------------------------------------------------------------
# CLI -- so shell entry points (run_goal.sh) can gate without a Python heredoc
# ---------------------------------------------------------------------------

def cmd_can_run(args: argparse.Namespace) -> int:
    allowed = can_run(args.platform)
    print("allowed" if allowed else "blocked")
    return 0 if allowed else 1


def cmd_remaining(args: argparse.Namespace) -> int:
    print(remaining_quota(args.platform))
    return 0


def cmd_next_slot(args: argparse.Namespace) -> int:
    slot = next_slot(args.platform)
    print(slot.isoformat() if slot else "-")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    record = record_publish(args.platform, args.job_id)
    print(json.dumps(record, ensure_ascii=False))
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="플랫폼 스케줄 + 일일 한도 조회/기록 (Phase 5)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    can_run_p = subparsers.add_parser("can-run", help="지금 실행 가능한지 (exit 0/1)")
    can_run_p.add_argument("platform")
    can_run_p.set_defaults(func=cmd_can_run)

    remaining_p = subparsers.add_parser("remaining", help="오늘 남은 쿼터 출력")
    remaining_p.add_argument("platform")
    remaining_p.set_defaults(func=cmd_remaining)

    next_slot_p = subparsers.add_parser("next-slot", help="다음 예정 시각 출력")
    next_slot_p.add_argument("platform")
    next_slot_p.set_defaults(func=cmd_next_slot)

    record_p = subparsers.add_parser("record", help="발행 기록 추가")
    record_p.add_argument("platform")
    record_p.add_argument("job_id")
    record_p.set_defaults(func=cmd_record)

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
