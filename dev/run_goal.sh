#!/bin/bash

# 사용법: ./run_goal.sh subscriber_growth [씨드 주제] [JOB_ID]
# 모드는 MODE 환경변수로 바꾼다:
#   MODE=auto   (기본) 사람 개입 없이 업로드까지
#   MODE=review 대본 검수 / 최종 승인 두 지점에서 멈춤
#   FORCE=1     dev/config/schedules.yaml의 daily_limit/시간대를 무시하고 강제 실행
set -e

OBJECTIVE="${1:-}"
SEED="${2:-}"
JOB_ID="${3:-auto_$(date +%Y%m%d_%H%M%S)}"
MODE="${MODE:-auto}"
FORCE="${FORCE:-0}"

if [ -z "$OBJECTIVE" ]; then
    echo "사용법: ./run_goal.sh subscriber_growth [씨드 주제] [JOB_ID]"
    exit 1
fi

export JOB_ID
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$BASE_DIR/config.sh"
source "$BASE_DIR/sh/common/notify.sh"

if [ "$FORCE" != "1" ] && ! python3 "$SRC_DIR/common/schedule_policy.py" can-run shorts; then
    echo "스케줄 정책(dev/config/schedules.yaml)에 따라 오늘 shorts 실행을 건너뜁니다. (강제 실행: FORCE=1)"
    exit 0
fi

trap 'notify_error "목표 기반 파이프라인 실패 (단계: ${CURRENT_STEP:-unknown}, JOB_ID: $JOB_ID)"' ERR

CURRENT_STEP="topic_plan"
PLAN_ARGS=(plan --objective "$OBJECTIVE" --job-id "$JOB_ID" --output "$WORK_DIR/topic_plan.json" --require-runnable)
if [ -n "$SEED" ]; then
    PLAN_ARGS+=(--seed "$SEED")
fi
python3 "$SRC_DIR/common/0_topic_plan.py" "${PLAN_ARGS[@]}"

# 이후 단계 순서와 단계별 점검은 pipeline_flow.STAGES 한 곳에만 정의되어 있다.
# 여기서 다시 나열하지 않는다.
CURRENT_STEP="pipeline"
set +e
python3 "$SRC_DIR/common/run_pipeline.py" --job-id "$JOB_ID" advance \
    --mode "$MODE" --topic-json "$WORK_DIR/topic_plan.json"
STATUS=$?
set -e

case "$STATUS" in
    0)
        python3 "$SRC_DIR/common/schedule_policy.py" record shorts "$JOB_ID" >/dev/null || true
        notify_success "목표 기반 영상 생성+업로드 완료 (JOB_ID: $JOB_ID)"
        ;;
    10)
        # 게이트 정지는 실패가 아니다. 승인 후 재개하면 이어서 진행한다.
        notify_success "목표 기반 파이프라인이 승인 대기 중입니다 (JOB_ID: $JOB_ID). 승인: run_pipeline.py approve --job-id $JOB_ID"
        ;;
    *)
        notify_error "목표 기반 파이프라인 실패 (단계: $CURRENT_STEP, JOB_ID: $JOB_ID)"
        exit "$STATUS"
        ;;
esac
