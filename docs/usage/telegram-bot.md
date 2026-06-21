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
- `/cancel`: 현재 작업 상태를 취소합니다.

## 승인 흐름

1. `/run 주제` 또는 `/trend 키워드` 후 `/pick 번호`
2. 스크립트 확인 후 `/approve`
3. TTS 음성 확인 후 `/approve` 또는 `/rerun tts`
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
