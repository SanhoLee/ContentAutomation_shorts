#!/bin/bash
# X refresh_token 유휴 폐기 방지용 cron 스크립트. 포스팅 여부와 무관하게
# 주기적으로 호출해 토큰을 살려둔다 (며칠 포스팅이 없으면 refresh_token이
# X 쪽에서 죽는 문제, KNOWN_ISSUES.md 참고).
set -e
DEV_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
source "$DEV_DIR/secrets.sh"
python3 "$DEV_DIR/src/common/x_auth.py" --keepalive
