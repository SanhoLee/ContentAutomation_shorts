#!/bin/bash
# Manual only. This is NOT a pipeline stage any more -- posting to X needs a
# human who has read the thread, so it is driven from the bot's approval
# prompt (or /x_post), never from pipeline_flow. Kept as the CLI path for
# posting a job's thread by hand on the server.
#
# Still exits 0 on failure: the YouTube video is already uploaded by the
# time anyone runs this, so a posting failure (billing, X API outage, token
# issue) is a warning to act on, not a reason to fail a shell chain.
source "$(dirname "$0")/../../config.sh"

echo "X 게시 중..."
if python3 "$SRC_DIR/common/adapters/x_poster.py" --job-id "$JOB_ID"; then
    echo "완료. X에 게시되었습니다."
else
    echo "경고: X 게시 실패 (YouTube 업로드는 이미 완료됨). /x_post 로 재시도하세요."
fi
exit 0
