#!/bin/bash
set -e
source "$(dirname "$0")/../../config.sh"

OBJECTIVE="${1:-balanced}"
SEED="${2:-}"
ARGS=(plan --objective "$OBJECTIVE" --job-id "$JOB_ID" --output "$WORK_DIR/topic_plan.json")
if [ -n "$SEED" ]; then
    ARGS+=(--seed "$SEED")
fi

python3 "$SRC_DIR/common/0_topic_plan.py" "${ARGS[@]}"
