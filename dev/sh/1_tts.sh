#!/bin/bash
set -e
source "$(dirname "$0")/../config.sh"

mkdir -p "$WORK_DIR" "$BACKUP_DIR"

if [ ! -f "$WORK_DIR/script.txt" ]; then
    echo "오류: $WORK_DIR/script.txt 파일이 없습니다. 먼저 0_script.sh를 실행하거나 script.txt를 준비하세요."
    exit 1
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
THIS_BACKUP="$BACKUP_DIR/$JOB_ID/$TIMESTAMP/tts"
FILES_TO_BACKUP=("voice.wav" "voice_raw.wav")
EXISTING=()

for f in "${FILES_TO_BACKUP[@]}"; do
    if [ -f "$WORK_DIR/$f" ]; then
        EXISTING+=("$WORK_DIR/$f")
    fi
done

if [ ${#EXISTING[@]} -gt 0 ]; then
    mkdir -p "$THIS_BACKUP"
    mv "${EXISTING[@]}" "$THIS_BACKUP/"
    echo "기존 TTS 파일 백업 완료 -> $THIS_BACKUP/"
fi

echo "1. TTS 생성 중..."
python3 "$SRC_DIR/1_tts.py"

echo "완료. $WORK_DIR/voice.wav 생성됨"
