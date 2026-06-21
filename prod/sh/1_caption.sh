#!/bin/bash
set -e
source "$(dirname "$0")/../config.sh"

mkdir -p "$WORK_DIR" "$BACKUP_DIR"

if [ ! -f "$WORK_DIR/script.txt" ]; then
    echo "오류: $WORK_DIR/script.txt 파일이 없습니다. 자막 생성을 위해 필요합니다."
    exit 1
fi

if [ ! -f "$WORK_DIR/voice.wav" ]; then
    echo "오류: $WORK_DIR/voice.wav 파일이 없습니다. 먼저 1_tts.sh를 실행하거나 voice.wav를 준비하세요."
    exit 1
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
THIS_BACKUP="$BACKUP_DIR/$JOB_ID/$TIMESTAMP/caption"
FILES_TO_BACKUP=("subs.srt" "scenes_timed.json")
EXISTING=()

for f in "${FILES_TO_BACKUP[@]}"; do
    if [ -f "$WORK_DIR/$f" ]; then
        EXISTING+=("$WORK_DIR/$f")
    fi
done

if [ ${#EXISTING[@]} -gt 0 ]; then
    mkdir -p "$THIS_BACKUP"
    mv "${EXISTING[@]}" "$THIS_BACKUP/"
    echo "기존 자막/타이밍 파일 백업 완료 -> $THIS_BACKUP/"
fi

echo "2. 자막 생성 중..."
python3 "$SRC_DIR/2_caption.py"

echo "완료. $WORK_DIR/subs.srt, scenes_timed.json 생성됨"
