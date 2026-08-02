#!/bin/bash
# Best-effort: this must never fail the whole job -- by this point the
# YouTube video is already uploaded, which is the primary deliverable. A
# posting failure (billing, X API outage, token issue) should surface as a
# warning the operator can act on with /x_post, not as a pipeline failure.
source "$(dirname "$0")/../../config.sh"

echo "X 게시 중..."
if python3 "$SRC_DIR/common/adapters/x_poster.py" --job-id "$JOB_ID"; then
    echo "완료. X에 게시되었습니다."
else
    echo "경고: X 게시 실패 (YouTube 업로드는 이미 완료됨). /x_post 로 재시도하세요."
fi
exit 0
