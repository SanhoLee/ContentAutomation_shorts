#!/bin/bash
set -e
source "$(dirname "$0")/../config.sh"

mkdir -p "$WORK_DIR"

echo "0. PubMed 리서치 + 대본/메타데이터 작성 중..."
python3 "$SRC_DIR/0_script.py"

echo "완료. $WORK_DIR/script.txt, scenes.json, video_meta.json 확인하세요."