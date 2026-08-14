#!/usr/bin/env python3
"""x_auth.py — X(Twitter) OAuth 2.0 token storage + refresh.

X rotates the refresh token on every use: each refresh call returns a new
access_token AND a new refresh_token, and the old refresh_token is
invalidated immediately. So unlike YOUTUBE_REFRESH_TOKEN (a static value in
secrets.sh), this token can't live as an env var -- it has to be persisted
to a file this module rewrites after every refresh. X_CLIENT_ID/
X_CLIENT_SECRET (the app-level, non-rotating credentials) still come from
secrets.sh as usual.

Bootstrap once with the access/refresh token obtained through the X
Developer Portal's interactive OAuth 2.0 consent flow:

    python x_auth.py --access-token ... --refresh-token ... --expires-in 7200

After that, get_valid_access_token() refreshes automatically when the
stored token is near expiry and callers never see a raw token file.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import time
from pathlib import Path
from typing import Any

TOKEN_ENDPOINT = "https://api.x.com/2/oauth2/token"
EXPIRY_BUFFER_SEC = 120
DEFAULT_EXPIRES_IN_SEC = 7200
REQUEST_TIMEOUT_SEC = 15


def _token_path() -> Path:
    configured = os.environ.get("X_TOKEN_FILE")
    if configured:
        return Path(configured).expanduser()
    dev_dir = Path(__file__).resolve().parents[2]  # .../dev/src/common -> dev
    return dev_dir / "data" / "x_token.json"


def _load(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save(path: Path, token: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(token, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def bootstrap(
    access_token: str, refresh_token: str, *, expires_in: int = DEFAULT_EXPIRES_IN_SEC,
    token_file: str | Path | None = None,
) -> Path:
    """One-time seed from a token obtained via the portal's interactive
    flow. Overwrites any existing token file for this environment."""
    path = Path(token_file) if token_file else _token_path()
    _save(path, {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": time.time() + int(expires_in),
    })
    return path


def _refresh(refresh_token: str) -> dict[str, Any]:
    import requests

    client_id = os.environ.get("X_CLIENT_ID")
    client_secret = os.environ.get("X_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError("X_CLIENT_ID/X_CLIENT_SECRET가 secrets.sh에 없습니다.")

    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    res = requests.post(
        TOKEN_ENDPOINT,
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data={"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": client_id},
        timeout=REQUEST_TIMEOUT_SEC,
    )
    res.raise_for_status()
    return res.json()


def get_valid_access_token(*, token_file: str | Path | None = None) -> str:
    """Bounded, non-retrying: a refresh failure raises rather than
    silently posting with a stale token or retrying (a retry that lands
    after a refresh actually succeeded server-side would burn the
    already-rotated refresh token)."""
    path = Path(token_file) if token_file else _token_path()
    token = _load(path)
    if token is None:
        raise RuntimeError(
            f"{path}에 X 토큰이 없습니다. "
            "먼저 `python x_auth.py --access-token ... --refresh-token ...`로 등록하세요."
        )

    if float(token.get("expires_at") or 0) - EXPIRY_BUFFER_SEC > time.time():
        return str(token["access_token"])

    refreshed = _refresh(token["refresh_token"])
    new_token = {
        "access_token": refreshed["access_token"],
        # X always returns a rotated refresh_token; fall back to the old
        # one only if a response is ever missing it, so a single odd
        # response doesn't strand the caller without any refresh path.
        "refresh_token": refreshed.get("refresh_token") or token["refresh_token"],
        "expires_at": time.time() + int(refreshed.get("expires_in") or DEFAULT_EXPIRES_IN_SEC),
    }
    _save(path, new_token)
    return new_token["access_token"]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="X OAuth 2.0 토큰 최초 등록 (bootstrap) / 주기적 갱신")
    parser.add_argument("--access-token")
    parser.add_argument("--refresh-token")
    parser.add_argument("--expires-in", type=int, default=DEFAULT_EXPIRES_IN_SEC, help="초 단위, 기본 2시간")
    parser.add_argument(
        "--keepalive", action="store_true",
        help="포스팅 여부와 무관하게 저장된 토큰을 갱신(cron용). refresh_token이 X 쪽에서 "
        "유휴 폐기되기 전에 주기적으로 호출해 살려둔다.",
    )
    args = parser.parse_args(argv)
    if not args.keepalive and (not args.access_token or not args.refresh_token):
        parser.error("--access-token/--refresh-token이 필요합니다 (최초 등록) 또는 --keepalive를 쓰세요")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.keepalive:
        get_valid_access_token()
        print("X 토큰 갱신(keepalive) 완료")
        return 0
    path = bootstrap(args.access_token, args.refresh_token, expires_in=args.expires_in)
    print(f"X 토큰 저장 완료: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
