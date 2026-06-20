#!/bin/bash
set -e
source "$(dirname "$0")/../config.sh"

mkdir -p "$WORK_DIR" "$BACKUP_DIR"

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
THIS_BACKUP="$BACKUP_DIR/$TIMESTAMP"

FILES_TO_BACKUP="voice.wav subs.srt broll.mp4"
EXISTING=""
for f in $FILES_TO_BACKUP; do
    if [ -f "$WORK_DIR/$f" ]; then EXISTING="$EXISTING $WORK_DIR/$f"; fi
done

if [ -n "$EXISTING" ]; then
    mkdir -p "$THIS_BACKUP"
    mv $EXISTING "$THIS_BACKUP/"
    echo "이전 파일 백업 완료 -> $THIS_BACKUP/"
fi

echo "1. TTS 생성 중..."
python3 "$SRC_DIR/1_tts.py"

echo "2. 자막 생성 중..."
python3 "$SRC_DIR/2_caption.py"

echo "3. B-roll 다운로드 중..."
python3 "$SRC_DIR/3_broll.py"

echo "완료. $WORK_DIR/subs.srt 확인/수정 후 2_render.sh 실행하세요."