# Slack Bot Workflow

Slack 봇은 기존 Telegram 봇과 동일한 승인형 파이프라인(`/run`, `/trend`, `/pick`, `/approve`, `/rerun`, `/render`, `/set`, `/app_status`, `/cancel`)을 실행합니다. 각 환경의 `slack_bot.py`에 단계 전환·산출물 처리·Slack 전송 로직이 모두 포함되어 Telegram 프로세스·토큰·소스코드 없이 독립 실행됩니다.

## Slack에서 추가된 동작

- 모든 작업 메시지와 산출물은 명령을 보낸 메시지의 **스레드**에 모입니다. 여러 작업이 섞이는 것을 줄일 수 있습니다.
- `SLACK_ALLOWED_USER_ID`를 설정하면 채널뿐 아니라 사용자도 제한할 수 있습니다. Telegram의 chat ID 제한보다 세밀합니다.
- Slack 파일 업로드로 `script.txt`, `subs.srt`, `video_meta.json` 수정본을 스레드에 올릴 수 있습니다.
- 각 검수 화면에는 현재 `n/7` 단계와 전체 진행 표시가 함께 나타납니다.
- 모든 검수 단계에서 `← 이전 단계`, `다음 단계 ▶`, `🚀 여기서부터 끝까지`, `↻ 상태`, `전체 취소`를 사용할 수 있습니다.
- `🚀 여기서부터 끝까지`는 확인 버튼을 한 번 더 누른 뒤 현재 산출물을 승인한 것으로 처리하고 남은 생성·렌더·비공개 업로드만 순서대로 실행합니다. 스크립트, 음성, 자막, B-roll, 렌더 설정, 최종 영상, 업로드 정보 어느 검수 단계에서든 시작할 수 있습니다.
- 버튼의 `전체 취소`도 확인 후 실행됩니다. 실행 중 취소를 확정하면 현재 프로세스까지 중단을 요청합니다. 이미 생성된 산출물은 삭제하지 않아 재시도와 원인 확인에 사용할 수 있습니다. 직접 입력한 `/cancel`은 즉시 취소 요청으로 처리합니다.

## 단계별 인터페이스

| 단계 | 기본 확인 방식 | 수정·복구 방식 |
|------|----------------|----------------|
| 트렌드 선택 | 후보별 Slack 버튼 | 다른 후보 선택 또는 전체 취소 |
| 스크립트 | 본문 미리보기와 원본 파일 | 본문 수정, 제목 수정, 이전 산출물을 덮어쓰는 재진행 |
| 음성 | Slack 오디오 파일 | 음성 재생성 또는 스크립트 단계로 돌아가기 |
| 자막 | 자막 미리보기와 `subs.srt` | 파일/텍스트 수정, 자막 재생성 또는 음성 단계로 돌아가기 |
| B-roll | Slack 동영상 파일 | B-roll 재생성 또는 자막 단계로 돌아가기 |
| 렌더 설정 | 현재 설정 요약과 프리셋 버튼 | 프리셋 선택, `/render` 세부 조정 또는 B-roll 단계로 돌아가기 |
| 최종 영상 | Slack 동영상 파일 | 렌더 설정으로 돌아가 다시 렌더 |
| 업로드 정보 | 메타데이터 미리보기와 JSON 파일 | 메타데이터 수정 또는 최종 영상 단계로 돌아가기 |

`/app_status`는 원시 상태 JSON 대신 동일한 진행 카드와 현재 단계에서 가능한 버튼을 다시 표시합니다. 오래된 메시지의 버튼을 누르면 현재 단계와 일치하는지 확인한 뒤 실행하므로 실수로 과거 작업을 진행하지 않습니다.

## 사전 준비

1. Slack API에서 앱을 만들고 **Socket Mode**를 켭니다.
2. App-Level Token을 만들고 `connections:write` scope를 부여합니다.
3. Bot Token Scopes에 다음을 추가합니다.
   - `chat:write`, `files:write`, `files:read`
   - 명령을 읽을 채널 유형에 맞는 `channels:history`, `groups:history`, `im:history`, `mpim:history`
4. Event Subscriptions에서 Socket Mode 이벤트로 `message.channels`(비공개 채널이면 `message.groups`)를 구독합니다. 봇을 대상 채널에 초대합니다.
5. Slash Commands에 아래 명령을 등록하고 Request URL은 Slack이 Socket Mode로 수신하도록 설정합니다: `/run`, `/run_auto`, `/trend`, `/pick`, `/approve`, `/edit`, `/retry`, `/proceed`, `/rerun`, `/render`, `/set`, `/set_all`, `/app_status`, `/cancel`, `/help`. Slack 예약 명령인 `/status`는 등록하지 않습니다.

`secrets.sh`에 토큰과 접근 범위를 넣습니다. 값은 저장소에 커밋하지 않습니다.

```bash
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."
export SLACK_CHANNEL_ID="C0123456789"       # 권장: 작업 채널 1개로 제한
export SLACK_ALLOWED_USER_ID="U0123456789"  # 선택: 특정 운영자만 허용
```

## 설치 및 실행

```bash
python3 -m pip install -r requirements-slack.txt
cd ~/brain50/dev
./sh/slack_bot.sh
```

운영 환경은 `prod` 경로에서 같은 방식으로 실행합니다. 상태는 `data/slack_state.json`에 보존되며 Telegram 상태와 분리됩니다. 기본 경로를 바꾸려면 `SLACK_STATE_PATH`를 설정합니다.

## systemd

```bash
./deploy/lightsail/install_slack_service.sh dev
./deploy/lightsail/restart_slack_service.sh dev
./deploy/lightsail/logs_slack_service.sh dev
./deploy/lightsail/stop_slack_service.sh dev
```

서비스 유닛의 기본 배포 경로는 기존 Telegram 유닛과 동일하게 `/home/ubuntu/brain50`입니다.
