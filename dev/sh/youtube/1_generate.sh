#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/../../config.sh"

# Visuals are planned here, not in 0_script.sh, so a script edited between the
# two stages is what the B-roll search actually sees.
"$SCRIPT_DIR/../common/scene_visuals.sh"
"$SCRIPT_DIR/1_tts.sh"
"$SCRIPT_DIR/1_caption.sh"
"$SCRIPT_DIR/1_broll.sh"

echo "완료. $WORK_DIR/subs.srt 확인/수정 후 2_render.sh 실행하세요."
