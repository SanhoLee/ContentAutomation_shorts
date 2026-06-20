#!/bin/bash

# 사용법: ./run.sh "주제 문장" [JOB_ID]
# 예시:
#   ./run.sh "오메가3가 정말 뇌에 좋을까?"
#   ./run.sh "오메가3가 정말 뇌에 좋을까?" test_v1

TOPIC="$1"
JOB_ID="${2:-$(date +%Y%m%d_%H%M%S)}"

export TOPIC

export JOB_ID

source "$(dirname "$0")/config.sh"
source "$(dirname "$0")/sh/notify.sh"

if [ -z "$TOPIC" ]; then
    echo "오류: 주제를 입력해주세요."
    echo "사용법: ./run.sh \"주제 문장\" [JOB_ID]"
    exit 1
fi

set -e
trap 'notify_error "파이프라인 실패 (단계: $CURRENT_STEP, line $LINENO)"' ERR

CURRENT_STEP="0_script"
"$BASE_DIR/sh/0_script.sh" "$TOPIC"

CURRENT_STEP="1_generate"
"$BASE_DIR/sh/1_generate.sh"

CURRENT_STEP="2_render"
"$BASE_DIR/sh/2_render.sh" "$@"

CURRENT_STEP="3_upload"
"$BASE_DIR/sh/3_upload.sh"

notify_success "영상 생성+업로드 완료 (JOB_ID: $JOB_ID, $(date +%Y%m%d_%H%M%S))"