#!/bin/bash
set -e
source "$(dirname "$0")/../config.sh"

TOPIC="${1:-${TOPIC:-}}"
if [ -z "$TOPIC" ]; then
    echo "오류: 주제를 입력해주세요."
    echo "사용법: ./sh/0_script.sh \"주제 문장\""
    exit 1
fi

mkdir -p "$WORK_DIR"

echo "0. PubMed 리서치 + 대본/메타데이터 작성 중..."
python3 "$SRC_DIR/0_script.py" "$TOPIC"

echo "완료. $WORK_DIR/script.txt, scenes.json, video_meta.json 확인하세요."
