#!/bin/bash
# 트렌드 조사 → 주제 후보 큐 갱신 → 상위 3개 추천 출력.
#
# 사용법:
#   ./sh/common/refresh_topics.sh                  조사 후 터미널에 상위 3개 출력
#   ./sh/common/refresh_topics.sh --notify-slack   추가로 슬랙에 선택 버튼 카드 게시
#
# 이 스크립트는 후보를 "고르지" 않는다. 선택은 사람이 별도로 한다:
#   python3 src/common/topic_candidate_pipeline.py --select-rank 1
#
# 스케줄 등록(systemd timer / crontab) 방법은 docs/usage/topic-scheduling.md 참고.
set -e
source "$(dirname "$0")/../../config.sh"

NOTIFY_SLACK=0
for arg in "$@"; do
    case "$arg" in
        --notify-slack) NOTIFY_SLACK=1 ;;
        *) echo "알 수 없는 인자: $arg" >&2; exit 1 ;;
    esac
done

PIPELINE="$SRC_DIR/common/topic_candidate_pipeline.py"

echo "1. 트렌드 조사 + 후보 큐 갱신 중..."
python3 "$PIPELINE" --refresh

echo
echo "2. 상위 3개 추천"
python3 "$PIPELINE" --top 3

if [ "$NOTIFY_SLACK" = "1" ]; then
    if [ -z "${SLACK_CHANNEL_ID:-}" ] || [ -z "${SLACK_BOT_TOKEN:-}" ]; then
        echo "SLACK_CHANNEL_ID/SLACK_BOT_TOKEN이 없어 슬랙 알림을 건너뜁니다."
    else
        echo
        echo "3. 슬랙에 선택 카드 게시"
        # slack_bot은 임포트 부작용이 없으므로(main()이 __main__ 뒤) 봇 루프 없이
        # 카드만 게시할 수 있다. 버튼 클릭은 돌고 있는 봇 프로세스가 처리한다.
        # PYTHONPATH는 config.sh가 common/youtube/instagram 셋 다 얹어준다.
        python3 -c "
import slack_bot
slack_bot.send_topic_candidates(slack_bot.ALLOWED_CHANNEL_ID, '오늘의 주제 후보를 조사했습니다.')
" || echo "슬랙 게시 실패 (후보 큐는 정상 갱신됨)"
    fi
fi
