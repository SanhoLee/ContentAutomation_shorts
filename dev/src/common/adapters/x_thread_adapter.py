#!/usr/bin/env python3
"""x_thread_adapter.py — content_package.json -> X(Twitter) thread draft.

Phase 4 of the Pareto design spec. Purely rule-based: every tweet comes from
a field content_package.py already computed, so this makes zero Claude
calls. Posting is out of scope here — this only writes a draft an operator
copies by hand (or a future phase wires to the X API).

    Tweet 1      : hook
    Tweet 2..N-1 : one key_point per tweet
    Tweet N      : cta.action (+ next_topic_tease as a question), hashtags

Sentences that trip the Phase 1 safety ban-list (topic_score's
ban_keywords — "완치", "보장", "기적", ...) are dropped rather than posted
with a softened claim, since a dropped sentence is trivially safe and a
softened one still risks an implied medical guarantee.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COMMON_DIR = Path(__file__).resolve().parents[1]
if str(COMMON_DIR) not in sys.path:
    sys.path.insert(0, str(COMMON_DIR))

import content_package
import topic_score

TWEET_MAX_CHARS = 270
MAX_HASHTAGS_LAST_TWEET = 2

SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def _ban_keywords() -> list[str]:
    try:
        return list(topic_score.load_rules().get("ban_keywords") or ())
    except Exception:
        return []


def _apply_safety_filter(text: str, ban_keywords: list[str]) -> str:
    """Drop a sentence outright if it trips a ban keyword -- no softened
    rewrite, since that still risks an implied guarantee slipping through."""
    if not text:
        return ""
    lowered = text.lower()
    if any(bad.lower() in lowered for bad in ban_keywords if bad):
        return ""
    return text


def _truncate_at_boundary(text: str, max_chars: int) -> str:
    """Cut at the last sentence end within max_chars, else the last space,
    else a hard cut. No ellipsis -- spec calls for "의미 단위로 축약", not "...". """
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    matches = list(SENTENCE_END.finditer(window))
    if matches:
        return window[: matches[-1].end()].strip()
    last_space = window.rfind(" ")
    if last_space > 0:
        return window[:last_space].strip()
    return window.strip()


def build_tweets(
    package: dict[str, Any], *, number_prefix: bool = False, ban_keywords: list[str] | None = None,
) -> list[dict[str, Any]]:
    ban_keywords = ban_keywords if ban_keywords is not None else _ban_keywords()
    raw_texts: list[str] = []

    hook = _apply_safety_filter(str(package.get("hook") or "").strip(), ban_keywords)
    if hook:
        raw_texts.append(hook)

    for point in package.get("key_points") or ():
        text = _apply_safety_filter(str((point or {}).get("text") or "").strip(), ban_keywords)
        if text:
            raw_texts.append(text)

    cta = package.get("cta") or {}
    action = str(cta.get("action") or "").strip()
    tease = str(cta.get("next_topic_tease") or "").strip()
    closing_parts = [p for p in (action, f"다음 편에서는 {tease} 이야기, 궁금하지 않으세요?" if tease else "") if p]
    closing = _apply_safety_filter(" ".join(closing_parts).strip(), ban_keywords)
    if closing:
        raw_texts.append(closing)

    hashtags = [tag for tag in (package.get("hashtags") or [])][:MAX_HASHTAGS_LAST_TWEET]

    tweets: list[dict[str, Any]] = []
    total = len(raw_texts)
    for i, text in enumerate(raw_texts, start=1):
        prefix = f"{i}/{total} " if number_prefix else ""
        suffix = f" {' '.join(hashtags)}" if (i == total and hashtags) else ""
        budget = max(1, TWEET_MAX_CHARS - len(prefix) - len(suffix))
        body = _truncate_at_boundary(text, budget)
        tweets.append({"index": i, "text": f"{prefix}{body}{suffix}".strip()})
    return tweets


def render_text(payload: dict[str, Any]) -> str:
    total = len(payload["tweets"])
    return "\n\n".join(f"[{t['index']}/{total}] {t['text']}" for t in payload["tweets"])


def build_x_thread(job_dir: str | Path, *, number_prefix: bool = False) -> dict[str, Any] | None:
    """Build/refresh x_thread.json (+ .txt) for one job directory.

    Returns None if content_package.json doesn't exist yet (Stage 2 hasn't
    finished, or content_package build failed) -- the caller should tell the
    operator to run the script stage first rather than guessing at a thread.
    """
    job_dir = Path(job_dir)
    package = content_package.load_content_package(job_dir)
    if package is None:
        return None

    tweets = build_tweets(package, number_prefix=number_prefix)
    payload = {
        "job_id": package.get("job_id") or job_dir.name,
        "tweets": tweets,
        "char_counts": [len(t["text"]) for t in tweets],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "rule_v1",
    }
    (job_dir / "x_thread.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    (job_dir / "x_thread.txt").write_text(render_text(payload) + "\n", encoding="utf-8")
    content_package.update_platform_flag(job_dir, "x_thread", True)
    return payload


def resolve_work_dir(job_id: str) -> Path:
    base = os.environ.get("WORK_DIR_BASE")
    if base:
        return Path(base) / job_id
    dev_dir = Path(__file__).resolve().parents[3]  # .../dev/src/common/adapters -> dev
    return dev_dir / "data" / "work" / job_id


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="content_package.json -> X 스레드 초안 (규칙 기반, Phase 4)")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--print", dest="print_output", action="store_true", help="생성 결과를 표준출력에도 출력")
    parser.add_argument("--number-prefix", action="store_true", help="트윗 앞에 1/ 2/ 형식 번호를 붙임")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    job_dir = resolve_work_dir(args.job_id)
    payload = build_x_thread(job_dir, number_prefix=args.number_prefix)
    if payload is None:
        print(f"content_package.json이 없습니다: {job_dir}. 먼저 스크립트 생성을 완료하세요.", file=sys.stderr)
        return 1
    print(f"x_thread.json 작성 완료: {job_dir / 'x_thread.json'} ({len(payload['tweets'])}개 트윗)")
    if args.print_output:
        print()
        print(render_text(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
