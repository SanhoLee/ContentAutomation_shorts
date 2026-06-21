#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../config.sh"

"$SCRIPT_DIR/1_tts.sh"
"$SCRIPT_DIR/1_caption.sh"
"$SCRIPT_DIR/1_broll.sh"

echo "완료. $WORK_DIR/subs.srt 확인/수정 후 2_render.sh 실행하세요."
