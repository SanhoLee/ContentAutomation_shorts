#!/bin/bash
# Runs after the script gate, on the text the operator actually approved:
# re-syncs scenes.json to script.txt and plans what each scene shows.
#
# Best-effort like x_thread.sh -- scene_visuals.py fills every field
# deterministically when the planning call fails, so the only way out here is
# a crash, and a crash must not throw away a finished, approved script.
source "$(dirname "$0")/../../config.sh"

echo "씬 시각 계획 생성 중..."
if python3 "$SRC_DIR/common/scene_visuals.py" --job-id "$JOB_ID"; then
    echo "완료. $WORK_DIR/scenes.json 확인하세요."
else
    echo "경고: 씬 시각 계획 실패 (기존 scenes.json으로 계속 진행합니다)."
fi
exit 0
