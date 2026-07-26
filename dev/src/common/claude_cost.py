"""Deterministic Claude usage pricing, persistence and budget guards."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from zoneinfo import ZoneInfo


MODEL_PRICES = {
    "haiku": {"input": 1.0, "output": 5.0},
    "sonnet": {"input": 3.0, "output": 15.0},
}
WEB_SEARCH_PRICE_USD = 0.01
CACHE_WRITE_MULTIPLIER = 1.25
CACHE_READ_MULTIPLIER = 0.10


def _model_family(model: str) -> str:
    lowered = str(model or "").lower()
    for family in MODEL_PRICES:
        if family in lowered:
            return family
    raise ValueError(f"지원하지 않는 Claude 모델 단가입니다: {model}")


def calculate_cost(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    web_search_requests: int = 0,
) -> float:
    prices = MODEL_PRICES[_model_family(model)]
    cost = (
        max(0, int(input_tokens)) / 1_000_000 * prices["input"]
        + max(0, int(output_tokens)) / 1_000_000 * prices["output"]
        + max(0, int(cache_creation_input_tokens)) / 1_000_000
        * prices["input"] * CACHE_WRITE_MULTIPLIER
        + max(0, int(cache_read_input_tokens)) / 1_000_000
        * prices["input"] * CACHE_READ_MULTIPLIER
        + max(0, int(web_search_requests)) * WEB_SEARCH_PRICE_USD
    )
    return round(cost, 10)


def usage_fields(response: Mapping[str, Any] | None) -> dict[str, int]:
    usage = (response or {}).get("usage") or {}
    return {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_creation_input_tokens": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read_input_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "web_search_requests": int(
            ((usage.get("server_tool_use") or {}).get("web_search_requests") or 0)
        ),
    }


def estimate_response_cost(model: str, response: Mapping[str, Any] | None) -> float:
    return calculate_cost(model, **usage_fields(response))


def ensure_usage_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS claude_usage (
            usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT,
            plan_id INTEGER,
            stage TEXT NOT NULL,
            attempt INTEGER NOT NULL DEFAULT 1,
            model TEXT NOT NULL,
            response_id TEXT,
            request_id TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
            cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
            web_search_requests INTEGER NOT NULL DEFAULT 0,
            estimated_cost_usd REAL NOT NULL DEFAULT 0,
            success INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)


def _default_db_path() -> Path:
    configured = os.environ.get("YOUTUBE_FEEDBACK_DB")
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parent.parent.parent / "data" / "youtube_feedback.db"


def record_usage(
    stage: str,
    model: str,
    response: Mapping[str, Any] | None,
    *,
    jsonl_path: str | Path | None = None,
    db_path: str | Path | None = None,
    job_id: str | None = None,
    plan_id: int | None = None,
    attempt: int = 1,
    success: bool = True,
) -> dict[str, Any]:
    fields = usage_fields(response)
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        estimated_cost = estimate_response_cost(model, response) if model else 0.0
        pricing_status = "estimated"
    except ValueError:
        # Custom/experimental models remain usable, but cannot silently borrow
        # another model's price. The zero estimate is explicitly marked.
        estimated_cost = 0.0
        pricing_status = "unknown_model"
    entry = {
        "ts": created_at,
        "job_id": job_id or os.environ.get("JOB_ID"),
        "plan_id": plan_id,
        "stage": str(stage),
        "attempt": int(attempt),
        "model": str(model),
        "response_id": (response or {}).get("id"),
        "request_id": (response or {}).get("_request_id"),
        "stop_reason": (response or {}).get("stop_reason"),
        **fields,
        "estimated_cost_usd": estimated_cost,
        "pricing_status": pricing_status,
        "success": bool(success),
    }
    if jsonl_path:
        target = Path(jsonl_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    target_db = Path(db_path) if db_path else _default_db_path()
    target_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target_db, timeout=5)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        ensure_usage_table(conn)
        conn.execute(
            """INSERT INTO claude_usage (
                job_id, plan_id, stage, attempt, model, response_id, request_id,
                input_tokens, output_tokens, cache_creation_input_tokens,
                cache_read_input_tokens, web_search_requests, estimated_cost_usd,
                success, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry["job_id"], plan_id, entry["stage"], entry["attempt"], model,
                entry["response_id"], entry["request_id"], fields["input_tokens"],
                fields["output_tokens"], fields["cache_creation_input_tokens"],
                fields["cache_read_input_tokens"], fields["web_search_requests"],
                entry["estimated_cost_usd"], 1 if success else 0, created_at,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return entry


TRUE_VALUES = {"1", "true", "on", "yes", "y"}
DEFAULT_BUDGET_TIMEZONE = "Asia/Tokyo"


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in TRUE_VALUES


def _budget_timezone() -> ZoneInfo:
    name = os.environ.get("CLAUDE_BUDGET_TIMEZONE", DEFAULT_BUDGET_TIMEZONE)
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo(DEFAULT_BUDGET_TIMEZONE)


def _created_day(created_at: str, tz: ZoneInfo) -> str:
    try:
        parsed = datetime.fromisoformat(str(created_at))
    except ValueError:
        return str(created_at)[:10]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(tz).date().isoformat()


def usage_total(
    *,
    db_path: str | Path | None = None,
    job_id: str | None = None,
    day: str | None = None,
    timezone_name: str | None = None,
) -> float:
    target = Path(db_path) if db_path else _default_db_path()
    if not target.exists():
        return 0.0
    conn = sqlite3.connect(target, timeout=5)
    try:
        ensure_usage_table(conn)
        clauses = []
        values: list[Any] = []
        if job_id is not None:
            clauses.append("job_id=?")
            values.append(job_id)
        if day is not None and timezone_name is None:
            clauses.append("substr(created_at, 1, 10)=?")
            values.append(day)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        if day is None or timezone_name is None:
            row = conn.execute(
                "SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM claude_usage" + where,
                values,
            ).fetchone()
            return float(row[0] or 0.0)
        tz = ZoneInfo(timezone_name)
        rows = conn.execute(
            "SELECT estimated_cost_usd, created_at FROM claude_usage" + where,
            values,
        ).fetchall()
        return float(sum(float(cost or 0.0) for cost, created_at in rows if _created_day(created_at, tz) == day))
    finally:
        conn.close()


def web_search_total(*, db_path: str | Path | None = None, job_id: str | None = None) -> int:
    target = Path(db_path) if db_path else _default_db_path()
    if not target.exists():
        return 0
    conn = sqlite3.connect(target, timeout=5)
    try:
        ensure_usage_table(conn)
        if job_id is None:
            row = conn.execute("SELECT COALESCE(SUM(web_search_requests), 0) FROM claude_usage").fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(SUM(web_search_requests), 0) FROM claude_usage WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def assert_budget(
    *,
    db_path: str | Path | None = None,
    job_id: str | None = None,
    job_budget_usd: float = 0.30,
    daily_budget_usd: float = 1.00,
    anticipated_cost_usd: float = 0.0,
) -> None:
    if _env_bool("CLAUDE_BUDGET_OVERRIDE"):
        return
    active_job = job_id or os.environ.get("JOB_ID")
    if active_job and usage_total(db_path=db_path, job_id=active_job) + anticipated_cost_usd > job_budget_usd:
        raise RuntimeError("Claude 작업 예산을 초과할 수 있어 자동 실행을 중단합니다.")
    budget_tz = _budget_timezone()
    today = datetime.now(timezone.utc).astimezone(budget_tz).date().isoformat()
    if usage_total(db_path=db_path, day=today, timezone_name=budget_tz.key) + anticipated_cost_usd > daily_budget_usd:
        raise RuntimeError("Claude 일일 예산을 초과할 수 있어 자동 실행을 중단합니다.")


# Compatibility-friendly names used by tests and callers.
calculate_claude_cost = calculate_cost
estimate_cost = calculate_cost
