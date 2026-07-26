#!/bin/bash
# 사용법: source notify.sh 후 notify_error "메시지"

send_telegram() {
    local message="$1"

    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
        echo "[WARN] Telegram 설정이 없어 알림을 건너뜁니다: $message" >&2
        return 0
    fi

    curl -s -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
        -d chat_id="${TELEGRAM_CHAT_ID}" \
        -d text="$message" > /dev/null || true
}

notify_error() {
    local message="$1"
    send_telegram "🚨 [Brain50] ${message}"
}

notify_success() {
    local message="$1"
    send_telegram "✅ [Brain50] ${message}"
}
