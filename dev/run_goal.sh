#!/bin/bash

# 사용법: ./run_goal.sh subscriber_growth [씨드 주제] [JOB_ID]
set -e

OBJECTIVE="${1:-}"
SEED="${2:-}"
JOB_ID="${3:-auto_$(date +%Y%m%d_%H%M%S)}"

if [ -z "$OBJECTIVE" ]; then
    echo "사용법: ./run_goal.sh subscriber_growth [씨드 주제] [JOB_ID]"
    exit 1
fi

export JOB_ID
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$BASE_DIR/config.sh"
source "$BASE_DIR/sh/notify.sh"

trap 'notify_error "목표 기반 파이프라인 실패 (단계: ${CURRENT_STEP:-unknown}, JOB_ID: $JOB_ID)"' ERR

CURRENT_STEP="topic_plan"
PLAN_ARGS=(plan --objective "$OBJECTIVE" --job-id "$JOB_ID" --output "$WORK_DIR/topic_plan.json" --require-runnable)
if [ -n "$SEED" ]; then
    PLAN_ARGS+=(--seed "$SEED")
fi
python3 "$SRC_DIR/0_topic_plan.py" "${PLAN_ARGS[@]}"

CURRENT_STEP="0_script"
"$BASE_DIR/sh/0_script.sh" --topic-json "$WORK_DIR/topic_plan.json"

CURRENT_STEP="1_generate"
"$BASE_DIR/sh/1_generate.sh"

CURRENT_STEP="2_render"
"$BASE_DIR/sh/2_render.sh"

CURRENT_STEP="3_upload"
"$BASE_DIR/sh/3_upload.sh"

notify_success "목표 기반 영상 생성+업로드 완료 (JOB_ID: $JOB_ID)"
