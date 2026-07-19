# Slack Bot Workflow

Slack 봇은 기존 Telegram 봇과 동일한 승인형 파이프라인(`/run`, `/trend`, `/pick`, `/approve`, `/rerun`, `/render`, `/set`, `/status`, `/cancel`)을 실행합니다. 각 환경의 `slack_bot.py`에 단계 전환·산출물 처리·Slack 전송 로직이 모두 포함되어 Telegram 프로세스·토큰·소스코드 없이 독립 실행됩니다.

## Slack에서 추가된 동작

- 모든 작업 메시지와 산출물은 명령을 보낸 메시지의 **스레드**에 모입니다. 여러 작업이 섞이는 것을 줄일 수 있습니다.
- `SLACK_ALLOWED_USER_ID`를 설정하면 채널뿐 아니라 사용자도 제한할 수 있습니다. Telegram의 chat ID 제한보다 세밀합니다.
- Slack 파일 업로드로 `script.txt`, `subs.srt`, `video_meta.json` 수정본을 스레드에 올릴 수 있습니다.

## 사전 준비

1. Slack API에서 앱을 만들고 **Socket Mode**를 켭니다.
2. App-Level Token을 만들고 `connections:write` scope를 부여합니다.
3. Bot Token Scopes에 다음을 추가합니다.
   - `chat:write`, `files:write`, `files:read`
   - 명령을 읽을 채널 유형에 맞는 `channels:history`, `groups:history`, `im:history`, `mpim:history`
4. Event Subscriptions에서 Socket Mode 이벤트로 `message.channels`(비공개 채널이면 `message.groups`)를 구독합니다. 봇을 대상 채널에 초대합니다.
5. Slash Commands에 아래 명령을 등록하고 Request URL은 Slack이 Socket Mode로 수신하도록 설정합니다: `/run`, `/run_auto`, `/trend`, `/pick`, `/approve`, `/edit`, `/retry`, `/proceed`, `/rerun`, `/render`, `/set`, `/set_all`, `/status`, `/cancel`, `/help`.

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
