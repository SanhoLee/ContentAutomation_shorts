#!/bin/bash
set -e
source "$(dirname "$0")/../config.sh"

mkdir -p "$WORK_DIR" "$BACKUP_DIR"

if [ ! -f "$WORK_DIR/scenes_timed.json" ]; then
    echo "오류: $WORK_DIR/scenes_timed.json 파일이 없습니다. 먼저 1_caption.sh를 실행하거나 scenes_timed.json을 준비하세요."
    exit 1
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
THIS_BACKUP="$BACKUP_DIR/$JOB_ID/$TIMESTAMP/broll"
FILES_TO_BACKUP=("broll.mp4" "broll_status.json")
EXISTING=()

for f in "${FILES_TO_BACKUP[@]}"; do
    if [ -f "$WORK_DIR/$f" ]; then
        EXISTING+=("$WORK_DIR/$f")
    fi
done

if [ ${#EXISTING[@]} -gt 0 ]; then
    mkdir -p "$THIS_BACKUP"
    mv "${EXISTING[@]}" "$THIS_BACKUP/"
    echo "기존 B-roll 파일 백업 완료 -> $THIS_BACKUP/"
fi

echo "3. B-roll 다운로드 중..."
python3 "$SRC_DIR/3_broll.py"

echo "완료. $WORK_DIR/broll.mp4 생성됨"
