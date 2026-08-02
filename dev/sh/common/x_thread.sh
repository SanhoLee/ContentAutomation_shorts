#!/bin/bash
# Best-effort: this must never fail the YouTube pipeline. x_thread.json is a
# downstream-only artifact (see x_thread_adapter.py) -- a Claude outage or a
# blown budget here should not block tts/caption/broll/render/upload.
source "$(dirname "$0")/../../config.sh"

echo "X 스레드 초안 생성 중..."
if python3 "$SRC_DIR/common/adapters/x_thread_adapter.py" --job-id "$JOB_ID"; then
    echo "완료. $WORK_DIR/x_thread.json 확인하세요."
else
    echo "경고: X 스레드 초안 생성 실패 (YouTube 파이프라인은 계속 진행합니다). 나중에 /x_thread 로 재시도하세요."
fi
exit 0
