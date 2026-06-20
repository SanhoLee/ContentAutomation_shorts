#!/bin/bash
set -e
source "$(dirname "$0")/../config.sh"

echo "4. YouTube 업로드 중..."
python3 "$SRC_DIR/4_upload.py"

echo "완료. YouTube Studio에서 비공개 영상 확인하세요."