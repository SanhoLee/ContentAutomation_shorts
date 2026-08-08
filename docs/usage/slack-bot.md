# Slack Bot Workflow

Slack 봇은 승인형 파이프라인(`/run`, `/trend`, `/pick`, `/approve`, `/rerun`, `/render`, `/set`, `/app_status`, `/cancel`)을 실행하는 유일한 드라이버입니다. `dev/src/common/slack_bot.py`에 단계 전환·산출물 처리·Slack 전송 로직이 모두 포함됩니다.

## Slack에서 추가된 동작

- 봇 서비스가 시작되면 `SLACK_CHANNEL_ID` 채널에 최상위 웰컴 홈을 게시합니다. `단계별 검수 제작`, `자동 제작`, `목표 기반 자동 기획`, `트렌드에서 시작`, `현재 작업`, `제작 설정` 버튼으로 진입할 수 있습니다.
- 제작 버튼은 즉시 파이프라인을 실행하지 않습니다. `제작 방식 선택 → 주제 입력 → 실행 확인`의 2단계 입력을 완료하고 `실행하기`를 눌러야 실제 작업이 시작됩니다.
- `목표 기반 자동 기획`은 `목표 선택 → 씨드 방식 선택 → 실행 확인` 순서로 진행합니다. 목표는 `구독자 증가`, `조회수·도달`, `평균 시청률`, `공유율 강화`, `균형 성장` 중에서 고릅니다. 씨드는 생략해 채널 데이터로 자동 선정하거나 다음 Slack 메시지로 직접 입력할 수 있습니다.
- 목표 기획은 실행 확인 전까지 기존 작업 상태를 바꾸지 않습니다. 실행하면 채널 성과를 분석해 주제를 선정하고, 스크립트 생성 후 기존 승인형 검수 단계에서 멈춥니다. 판단이 `manual_review` 또는 `rejected`이면 기획 결과만 보존하고 제작을 시작하지 않습니다.
- `/run 주제`, `/run_auto 주제`, `/trend 주제`로 직접 입력해도 즉시 실행되지 않고 같은 실행 확인 화면을 거칩니다. 주제 없이 명령하면 주제 입력 단계가 열립니다.
- 주제 입력과 실행 확인 화면에는 `← 홈으로`와 `시작 취소`가 있습니다. 기존 작업을 보던 중 새 제작 버튼을 잘못 눌러도 홈으로 돌아가면 기존 작업 상태가 유지됩니다.
- 모든 작업 메시지와 산출물은 명령을 보낸 메시지의 **스레드**에 모입니다. 여러 작업이 섞이는 것을 줄일 수 있습니다.
- `SLACK_ALLOWED_USER_ID`를 설정하면 채널뿐 아니라 사용자도 제한할 수 있습니다.
- X 스레드 초안이 만들어지면 봇이 **첫 트윗에 붙일 이미지를 첨부해 달라고 요청**합니다. 원하는 도구(ChatGPT 등)로 만들어 그 스레드에 그대로 올리면 저장되고, 최종 승인 화면에서 영상·초안과 함께 보여줍니다. 렌더가 도는 동안 올리면 되고, 안 올리면 자동 생성 글자 카드로 나갑니다. 다시 올리고 싶으면 `/x_photo`를 쓰세요. png·jpg·jpeg·webp만 받고 5MB를 넘으면 거절합니다.
- X 스레드가 게시되면 그 근거(PubMed 링크 등)는 스레드 트윗이 아니라 운영자 **DM**으로 갑니다. 그대로 복사해서 쓰면 됩니다. 수신자는 `SLACK_DM_USER_ID`(미설정 시 `SLACK_ALLOWED_USER_ID`)이며, job당 한 번만 보냅니다. 봇에 DM을 보내려면 `im:write` 스코프가 필요합니다.
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
4. Event Subscriptions에서 Socket Mode 이벤트로 `message.channels`(비공개 채널이면 `message.groups`)와 `app_home_opened`를 구독합니다. 봇을 대상 채널에 초대합니다.
5. Slash Commands를 함께 사용할 경우 아래 명령을 등록하고 Request URL은 Slack이 Socket Mode로 수신하도록 설정합니다: `/run`, `/run_auto`, `/run_goal`, `/trend`, `/pick`, `/approve`, `/edit`, `/retry`, `/proceed`, `/rerun`, `/render`, `/set`, `/set_all`, `/goal_status`, `/goal_report`, `/app_status`, `/cancel`, `/help`. 버튼만 사용할 때는 Slash Commands 등록이 필요하지 않습니다. Slack 예약 명령인 `/status`는 등록하지 않습니다.
6. Slack 앱 설정의 **App Home**에서 Home Tab을 활성화합니다. 앱을 직접 열었을 때도 채널 웰컴 카드와 동일한 제작 홈을 사용할 수 있습니다.

`secrets.sh`에 토큰과 접근 범위를 넣습니다. 값은 저장소에 커밋하지 않습니다.

```bash
export SLACK_BOT_TOKEN="xoxb-..."
export SLACK_APP_TOKEN="xapp-..."
export SLACK_CHANNEL_ID="C0123456789"       # 웰컴 홈·App Home 작업 대상 채널로 필수
export SLACK_ALLOWED_USER_ID="U0123456789"  # 선택: 특정 운영자만 허용
export SLACK_DM_USER_ID="U0123456789"       # 선택: X 스레드 출처 DM 수신자 (기본값은 위 값)
```

## 설치 및 실행

```bash
python3 -m pip install -r requirements-slack.txt
cd ~/brain50/dev
./sh/slack_bot.sh
```

상태는 `data/slack_state.json`에 보존됩니다. 기본 경로를 바꾸려면 `SLACK_STATE_PATH`를 설정합니다.

## systemd

```bash
./deploy/lightsail/install_slack_service.sh dev
./deploy/lightsail/restart_slack_service.sh dev
./deploy/lightsail/logs_slack_service.sh dev
./deploy/lightsail/stop_slack_service.sh dev
```

목표 기반 기획을 처음 수동으로 확인하려면 서비스를 재시작한 뒤 Slack에서 앱 Home 또는 채널의 새 홈 카드에서 다음 순서로 누릅니다.

```text
목표 기반 자동 기획
→ 구독자 증가
→ 씨드 없이 자동 선정
→ 실행하기
```

기존 메시지에 게시된 홈 카드는 자동 갱신되지 않습니다. 서비스를 재시작한 뒤 기존 카드의 `⌂ 홈 새로고침`을 누르거나, 앱 Home을 다시 열거나, 등록돼 있다면 `/help`를 입력해 새 홈 카드에서 버튼을 사용합니다.

버튼, 슬래시 명령, 메시지 입력, 백그라운드 작업 및 셸 명령의 요청·완료·실패 상태는 별도 파일 설정 없이 서비스 표준 출력에 기록됩니다. 위 `logs_slack_service.sh` 명령으로 `slack_action_requested`, `slack_action_finished`, `slack_task_finished`, `slack_command_failed` 등의 이벤트와 작업 ID·현재 단계를 바로 확인할 수 있습니다. 메시지 본문과 주제 원문은 로그에 남기지 않습니다.

서비스 유닛의 기본 배포 경로는 `/home/ubuntu/brain50`입니다.
