#!/bin/bash
set -e
source "$(dirname "$0")/../../config.sh"

echo "Instagram 카드 콘텐츠 생성 중 (스켈레톤)..."
python3 "$SRC_DIR/instagram/run.py"

echo "완료. $WORK_DIR/instagram_cards/, $WORK_DIR/instagram_cards.json 확인하세요."
