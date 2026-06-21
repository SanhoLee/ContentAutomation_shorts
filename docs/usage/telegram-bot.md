# Telegram Bot Workflow

텔레그램에서 단계별 승인형 파이프라인을 실행하는 최소 기능 봇입니다.

## 실행

개발 환경:

```bash
cd ~/brain50/dev
./sh/telegram_bot.sh
```

운영 환경:

```bash
cd ~/brain50/prod
./sh/telegram_bot.sh
```

`secrets.sh`에는 최소한 아래 값이 필요합니다.

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
```

`TELEGRAM_CHAT_ID`를 설정하면 해당 chat_id 외의 요청은 거부합니다.

봇은 시작 시 `/help`, `/run`, `/approve` 같은 명령을 텔레그램 앱의 명령어 메뉴에도 등록합니다.

## 기본 명령

잘못된 명령이나 잘못된 렌더 값이 들어오면 오류 메시지를 보내고 현재 작업 상태를 유지합니다.

- `/run 주제`: 승인형 파이프라인을 시작합니다.
- `/run_auto 주제`: 승인 없이 전체 파이프라인을 실행합니다.
- `/trend 키워드`: Google/YouTube 기반 후보를 조회합니다.
- `/pick 번호`: 트렌드 후보를 선택하고 스크립트를 생성합니다.
- `/approve`: 현재 산출물을 승인하고 다음 단계로 넘어갑니다.
- `/rerun tts`: TTS를 다시 생성합니다.
- `/rerun caption`: 자막을 다시 생성합니다.
- `/rerun broll`: B-roll을 다시 생성합니다.
- `/render font_size=22 margin_v=180`: 자막 렌더 설정을 바꿔 렌더링합니다.
- `/status`: 현재 작업 상태를 확인합니다.
- `/cancel`: 전체 작업 상태를 취소합니다.

## 승인 흐름

1. `/run 주제` 또는 `/trend 키워드` 후 `/pick 번호`
2. 스크립트 확인 후 `/approve`
3. TTS 음성 확인 후 `/approve`, `/rerun tts`, 또는 `스크립트 수정` 버튼
4. 자막 확인/수정 후 `/approve` 또는 `/rerun caption`
5. B-roll 확인 후 `/approve` 또는 `/rerun broll`
6. 렌더 설정 확인 후 `/approve` 또는 `/render font_size=22 margin_v=180`
7. 최종 영상 확인 후 `/approve`
8. 제목, 요약, 설명, 해시태그 확인 후 `/approve`
9. YouTube 비공개 업로드 실행

## 파일 위치

봇은 기존 shell 스크립트를 그대로 호출하고, 모든 산출물은 `data/work/{JOB_ID}/`에 저장합니다.

- 상태 파일: `data/telegram_state.json`
- 스크립트: `data/work/{JOB_ID}/script.txt`
- 음성: `data/work/{JOB_ID}/voice.wav`
- 자막: `data/work/{JOB_ID}/subs.srt`
- B-roll: `data/work/{JOB_ID}/broll.mp4`
- 최종 영상: `data/output/output_{JOB_ID}.mp4`
- 업로드 메타데이터: `data/work/{JOB_ID}/video_meta.json`

## 렌더 기본값

`config.yaml`에서 기본 렌더 자막 설정을 관리합니다.

- `CAPTION_FONT_SIZE`: 자막 폰트 크기
- `CAPTION_MARGIN_V`: 자막 수직 위치

텔레그램에서 별도 조정 없이 `/approve`하면 기본값으로 렌더링합니다.

## Lightsail 상시 실행

`./sh/telegram_bot.sh`를 SSH나 VSCode 터미널에서 직접 실행하면 해당 터미널 세션이 끊길 때 같이 종료될 수 있습니다. 노트북을 끄거나 VSCode를 닫아도 계속 실행하려면 Lightsail 서버에서 `systemd` 서비스로 등록하세요.

개발 환경 서비스를 등록하는 예시입니다.

```bash
sudo cp ~/brain50/deploy/systemd/brain50-telegram-dev.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now brain50-telegram-dev.service
sudo systemctl status brain50-telegram-dev.service
```

운영 환경은 prod 서비스 파일을 사용합니다.

```bash
sudo cp ~/brain50/deploy/systemd/brain50-telegram-prod.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now brain50-telegram-prod.service
sudo systemctl status brain50-telegram-prod.service
```

로그 확인:

```bash
journalctl -u brain50-telegram-dev.service -f
journalctl -u brain50-telegram-prod.service -f
```

서비스 중지:

```bash
sudo systemctl stop brain50-telegram-dev.service
sudo systemctl disable brain50-telegram-dev.service
```

## PubMed 검색 실패 처리

트렌드 후보나 사용자가 고른 주제가 PubMed에서 직접 검색되지 않아도 스크립트 생성은 계속 진행합니다. 대신 봇은 PubMed 직접 근거가 없었다는 안내와 로그를 먼저 보여줍니다.

- 로그 파일: `data/work/{JOB_ID}/pubmed_status.json`
- 새 주제로 다시 생성: `/retry 오메가3 기억력`

PubMed 결과가 없는 경우 Claude는 논문 수치나 특정 연구 결과를 지어내지 않고, 자체 지식 범위의 신빙성 높은 일반 의학 정보와 생활 맥락 중심으로 조심스럽게 작성하도록 지시합니다. 주제가 너무 짧거나, 너무 구체적이거나, `추천`, `가격`, `후기`, `고르는법`처럼 소비자 검색어에 가까운 경우 PubMed 검색이 약할 수 있습니다. 이때는 `오메가3 기억력`, `수면 부족 치매 위험`, `근력 운동 인지기능`처럼 연구 주제형 키워드로 바꿔보세요.

## 봇이 꺼져 있을 때

봇 프로세스가 완전히 꺼져 있으면 텔레그램 메시지를 읽을 주체가 없으므로 `/run ...`에 대해 "서버가 꺼져 있다"는 답장을 보낼 수 없습니다. 이 알림은 같은 서버 안의 bot만으로는 불가능합니다.

대신 아래 구조로 대응합니다.

- `systemd`의 `Restart=always`로 프로세스가 죽으면 자동 재시작합니다.
- `systemctl status brain50-telegram-dev.service`로 현재 실행 상태를 확인합니다.
- 외부 알림이 필요하면 UptimeRobot, GitHub Actions cron, 별도 서버 같은 외부 헬스체크가 `getMe` 또는 서버 healthcheck를 주기적으로 확인해야 합니다.

## Claude API 타임아웃 처리

스크립트 생성 단계에서 `api.anthropic.com` `ReadTimeout`이 발생하면 주제 검색 실패가 아니라 Claude 응답 생성이 설정 시간 안에 끝나지 않은 상황입니다. 기본값은 아래처럼 분리되어 있습니다.

- 일반 HTTP 검색 타임아웃: `REQUEST_TIMEOUT=20`
- Claude 응답 타임아웃: `CLAUDE_TIMEOUT=180`
- Claude 일시 오류 재시도: `CLAUDE_RETRIES=2`

Lightsail에서 반복적으로 같은 오류가 나면 `.env` 또는 systemd 환경변수에 `CLAUDE_TIMEOUT=300`처럼 더 크게 설정한 뒤 봇 서비스를 재시작하세요. 텔레그램에서는 같은 트렌드 후보를 다시 고르려면 `/pick 1`을 다시 보내고, 주제를 바꿔 재생성하려면 `/retry 오메가3 기억력`처럼 입력하면 됩니다.

## 텔레그램 수정 모드

승인이 필요한 단계에는 버튼이 함께 표시됩니다. 텍스트 산출물은 `수정` 버튼 또는 `/edit`으로 수정 모드에 들어갈 수 있습니다.

- 스크립트: `script.txt`를 수정해서 다시 업로드하면 다음 TTS 단계가 수정본을 사용합니다.
- 자막: `subs.srt`를 수정해서 다시 업로드하면 렌더 단계가 수정본을 사용합니다.
- 업로드 메타데이터: `video_meta.json`을 수정해서 다시 업로드하면 YouTube 업로드 단계가 수정본을 사용합니다.

TTS, B-roll, 최종 렌더처럼 텍스트 파일을 직접 고치는 것보다 재생성이 자연스러운 단계는 버튼으로 재생성하거나 렌더 프리셋을 선택합니다. 텔레그램은 봇이 보낸 메시지를 사용자가 직접 인라인 편집하게 만들 수 없으므로, 원본 파일을 열어 수정한 뒤 다시 보내는 방식으로 처리합니다.

## Lightsail 서비스 스크립트

Lightsail에서 봇을 상시 실행하려면 아래 스크립트를 사용합니다.

```bash
# dev 봇 설치 및 즉시 시작
./deploy/lightsail/install_telegram_service.sh dev

# prod 봇 설치 및 즉시 시작
./deploy/lightsail/install_telegram_service.sh prod

# 재시작
./deploy/lightsail/restart_telegram_service.sh dev

# 로그 확인
./deploy/lightsail/logs_telegram_service.sh dev

# 중지 및 자동 실행 해제
./deploy/lightsail/stop_telegram_service.sh dev
```

기본 경로는 `~/brain50`입니다. 다른 위치에 배포했다면 `APP_ROOT=/path/to/brain50 ./deploy/lightsail/install_telegram_service.sh dev`처럼 실행하세요.


## 실행 중 입력 처리

스크립트 생성, TTS, 자막, B-roll, 렌더링, 업로드처럼 시간이 걸리는 작업은 백그라운드로 실행됩니다. 실행 중에는 다른 버튼이나 명령을 눌러도 새 작업을 시작하지 않고 `현재 진행 중입니다` 안내만 보냅니다. `/status`는 실행 중에도 확인할 수 있습니다.

TTS 결과가 마음에 들지 않으면 TTS 승인 단계에서 `스크립트 수정` 버튼을 눌러 `script.txt` 수정 단계로 돌아갈 수 있습니다. 띄어쓰기, 숫자 표기, 조사 등을 고친 뒤 승인하면 TTS를 다시 생성합니다. `전체 취소`는 현재 JOB 전체를 취소하는 동작입니다.


## 단계 승인 보강

각 승인 버튼은 버튼이 만들어진 단계 정보를 함께 갖습니다. 이미 지난 단계의 오래된 버튼을 눌러도 현재 단계와 맞지 않으면 무효 처리됩니다. 다음 단계는 반드시 현재 단계의 승인 버튼이나 `/approve` 명령으로만 실행됩니다.

모든 승인 단계에는 가능한 경우 `이전 단계` 버튼이 표시됩니다. TTS가 마음에 들지 않으면 스크립트 단계로 돌아가 `script.txt`를 고친 뒤 다시 승인하고, 자막/B-roll/렌더/메타데이터 단계에서도 바로 앞 단계로 돌아가 확인을 다시 할 수 있습니다.
