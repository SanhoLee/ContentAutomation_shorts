#!/bin/bash
# 실패한 씬만 다시 검색하는 B-roll 보완 실행.
# 1_broll.sh 전체 재실행보다 Pexels 호출이 적어, 자동 재시도 경로에서 사용한다.
set -e
source "$(dirname "$0")/../../config.sh"

if [ ! -f "$WORK_DIR/broll_status.json" ]; then
    echo "오류: $WORK_DIR/broll_status.json 파일이 없습니다. 먼저 1_broll.sh를 실행하세요."
    exit 1
fi

echo "3b. 실패한 B-roll 장면 재시도 중..."
python3 "$SRC_DIR/youtube/3b_retry_broll.py"

echo "완료. $WORK_DIR/broll.mp4 갱신됨"
