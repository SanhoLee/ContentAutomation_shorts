#!/bin/bash
set -e
source "$(dirname "$0")/../config.sh"

if [ "$#" -gt 0 ]; then
    SCRIPT_ARGS=("$@")
elif [ -n "${TOPIC:-}" ]; then
    SCRIPT_ARGS=("$TOPIC")
else
    echo "오류: 주제를 입력해주세요."
    echo "사용법: ./sh/0_script.sh \"주제 문장\""
    echo "트렌드 후보: ./sh/0_script.sh --trend \"키워드\""
    echo "후보 선택: ./sh/0_script.sh --trend-choice 1"
    exit 1
fi

mkdir -p "$WORK_DIR"

echo "0. PubMed 리서치 + 대본/메타데이터 작성 중..."
python3 "$SRC_DIR/0_script.py" "${SCRIPT_ARGS[@]}"

if [ "${SCRIPT_ARGS[0]}" = "--trend" ]; then
    echo "완료. $WORK_DIR/trend_candidates.json 후보를 확인하세요."
else
    echo "완료. $WORK_DIR/script.txt, scenes.json, video_meta.json 확인하세요."
fi
