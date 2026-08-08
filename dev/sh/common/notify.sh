#!/bin/bash
# 사용법: source notify.sh 후 notify_error "메시지"

send_slack() {
    local message="$1"

    if [ -z "${SLACK_BOT_TOKEN:-}" ] || [ -z "${SLACK_CHANNEL_ID:-}" ]; then
        echo "[WARN] Slack 설정이 없어 알림을 건너뜁니다: $message" >&2
        return 0
    fi

    curl -s -X POST "https://slack.com/api/chat.postMessage" \
        -H "Authorization: Bearer ${SLACK_BOT_TOKEN}" \
        -H "Content-type: application/json; charset=utf-8" \
        --data "$(printf '{"channel":"%s","text":%s}' "$SLACK_CHANNEL_ID" \
            "$(printf '%s' "$message" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')")" \
        > /dev/null || true
}

notify_error() {
    local message="$1"
    send_slack "🚨 [Brain50] ${message}"
}

notify_success() {
    local message="$1"
    send_slack "✅ [Brain50] ${message}"
}
