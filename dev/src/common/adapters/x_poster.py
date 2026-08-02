#!/usr/bin/env python3
"""x_poster.py — x_thread.json -> 실제 X(Twitter) 게시.

Posts each tweet in x_thread.json as a reply chain so it renders as one
thread. Bounded, non-retrying: a failed request is not retried
automatically, since retrying a POST that actually succeeded server-side
but timed out on the client would double-post a tweet -- and a duplicate
tweet on a live account is much harder to clean up than a paused job.

Progress is persisted back into x_thread.json after every single
successful post (not just at the end), so a crash or failure mid-thread
leaves an accurate record of what's already live. Re-running the same
job resumes from the first unposted tweet rather than reposting the
whole thread. A thread already marked "posted": true refuses to run
again unless the operator clears that field by hand -- this is the one
guard against silently double-posting an entire thread.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COMMON_DIR = Path(__file__).resolve().parents[1]
if str(COMMON_DIR) not in sys.path:
    sys.path.insert(0, str(COMMON_DIR))

import x_auth

POST_ENDPOINT = "https://api.x.com/2/tweets"
POST_TIMEOUT_SEC = int(os.environ.get("X_POST_TIMEOUT_SEC", "20"))


def _thread_path(job_dir: Path) -> Path:
    return job_dir / "x_thread.json"


def _load_thread(job_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads(_thread_path(job_dir).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_thread(job_dir: Path, payload: dict[str, Any]) -> None:
    _thread_path(job_dir).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


def _post_one(text: str, *, reply_to: str | None, access_token: str) -> str:
    import requests

    body: dict[str, Any] = {"text": text}
    if reply_to:
        body["reply"] = {"in_reply_to_tweet_id": reply_to}
    res = requests.post(
        POST_ENDPOINT,
        headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"},
        json=body,
        timeout=POST_TIMEOUT_SEC,
    )
    res.raise_for_status()
    return str(res.json()["data"]["id"])


def post_thread(job_dir: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    job_dir = Path(job_dir)
    payload = _load_thread(job_dir)
    if payload is None:
        raise RuntimeError(f"x_thread.json이 없습니다: {job_dir}. 먼저 x_thread_adapter를 실행하세요.")

    if payload.get("posted"):
        raise RuntimeError(
            f"이미 게시된 스레드입니다 (posted_at={payload.get('posted_at')}, "
            f"thread_url={payload.get('thread_url')}). "
            "재게시하려면 x_thread.json에서 posted/tweet_ids를 수동으로 지우세요."
        )

    tweets = payload.get("tweets") or []
    posted_ids: list[str] = list(payload.get("tweet_ids") or [])
    resume_from = len(posted_ids)

    if dry_run:
        print(f"[dry-run] 총 {len(tweets)}개 트윗 중 {resume_from}개 게시된 상태 -- 실제 게시는 하지 않음")
        for i, tweet in enumerate(tweets, start=1):
            marker = "✓ 게시됨" if i <= resume_from else "  대기"
            print(f"  [{marker}] {i}. {tweet['text']}")
        return payload

    if resume_from >= len(tweets):
        raise RuntimeError("게시할 트윗이 남아있지 않습니다 (tweets 목록이 비어있거나 이미 모두 게시됨).")

    access_token = x_auth.get_valid_access_token()
    reply_to = posted_ids[-1] if posted_ids else None

    for tweet in tweets[resume_from:]:
        try:
            tweet_id = _post_one(tweet["text"], reply_to=reply_to, access_token=access_token)
        except Exception as exc:
            payload["tweet_ids"] = posted_ids
            payload["post_error"] = str(exc)
            _write_thread(job_dir, payload)
            raise RuntimeError(
                f"{len(posted_ids)}/{len(tweets)}개 게시 후 실패: {exc}. "
                f"재실행하면 {len(posted_ids) + 1}번째 트윗부터 이어서 게시합니다."
            ) from exc
        posted_ids.append(tweet_id)
        reply_to = tweet_id
        # Persist after every tweet, not just at the end -- a crash here
        # must not lose track of what's already live on the account.
        payload["tweet_ids"] = posted_ids
        _write_thread(job_dir, payload)

    payload["posted"] = True
    payload["posted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload["thread_url"] = f"https://x.com/i/web/status/{posted_ids[0]}"
    payload.pop("post_error", None)
    _write_thread(job_dir, payload)
    return payload


def resolve_work_dir(job_id: str) -> Path:
    base = os.environ.get("WORK_DIR_BASE")
    if base:
        return Path(base) / job_id
    dev_dir = Path(__file__).resolve().parents[3]  # .../dev/src/common/adapters -> dev
    return dev_dir / "data" / "work" / job_id


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="x_thread.json -> 실제 X 게시")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--dry-run", action="store_true", help="실제 게시 없이 현재 상태만 출력")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    job_dir = resolve_work_dir(args.job_id)
    try:
        payload = post_thread(job_dir, dry_run=args.dry_run)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not args.dry_run:
        print(f"게시 완료: {payload.get('thread_url')} ({len(payload.get('tweet_ids') or [])}개 트윗)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
