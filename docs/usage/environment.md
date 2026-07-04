# 개발/운영 환경 구분

현재 파이프라인은 `dev`와 `prod`를 같은 실행 인터페이스로 유지하되, 소스와 산출물 위치를 환경별로 분리합니다.

## 기본 배치

```text
~/brain50/
  dev/
    config.sh
    config.yaml
    src/
    sh/
    data/
      assets/
      work/{JOB_ID}/
      output/output_{JOB_ID}.mp4
  prod/
    config.sh
    config.yaml
    src/
    sh/
    data/
      assets/
      work/{JOB_ID}/
      output/output_{JOB_ID}.mp4
```

## 설정 파일

각 환경의 `config.yaml`에서 경로와 실행 파라미터를 관리합니다.

- `PROJECT_ROOT`: 기본 프로젝트 루트 (`/home/ubuntu/brain50`)
- `BASE_DIR`: 환경별 루트 (`${PROJECT_ROOT}/dev`, `${PROJECT_ROOT}/prod`)
- `SRC_DIR`: Python 소스 위치
- `ASSETS_DIR`: BGM, 설명 템플릿 등 assets 위치
- `WORK_DIR_BASE`: JOB_ID별 중간 산출물 위치
- `OUTPUT_DIR`: 최종 mp4 저장 위치
- `ATEMPO`, `TARGET_DURATION_SEC`, `CHARS_PER_SEC`: 콘텐츠 길이와 TTS 속도 관련 값



## TTS 실행 파일 확인

TTS 단계는 기본적으로 `supertonic` 실행 파일을 사용합니다. Lightsail에서 systemd로 봇을 실행하면 로그인 shell과 PATH가 달라 `supertonic`을 못 찾을 수 있습니다.

```bash
which supertonic
ls ~/.local/bin/supertonic
```

설치되어 있지만 다른 위치에 있다면 `dev/secrets.sh` 또는 `prod/secrets.sh`에 절대 경로를 지정하세요.

```bash
export TTS_BIN=/home/ubuntu/.local/bin/supertonic
```

설치 자체가 없다면 먼저 supertonic CLI를 설치해야 합니다. `ATEMPO`가 1.0이 아니면 `ffmpeg`도 필요합니다.

## 실행

개발 테스트:

```bash
cd ~/brain50/dev
./run.sh "테스트 주제" test_job_001
```

운영 콘텐츠 제작:

```bash
cd ~/brain50/prod
./run.sh "실제 업로드할 주제" prod_20250621_001
```

## 수동 확인 워크플로우

전체 자동 실행 대신 단계별 확인이 필요하면 같은 JOB_ID를 export한 뒤 개별 스크립트를 실행합니다.

```bash
cd ~/brain50/dev
export JOB_ID=test_job_001
source ./config.sh
./sh/0_script.sh "테스트 주제"
./sh/1_generate.sh
# subs.srt 확인/수정
./sh/2_render.sh
./sh/3_upload.sh
```

짧은 렌더 테스트가 필요하면 `2_render.sh`에 초 단위 길이를 전달합니다.

```bash
./sh/2_render.sh 10
```

이미 만들어진 `data/work/{JOB_ID}`의 `voice.wav`, `subs.srt`, `broll.mp4`를 그대로 써서 자막 스타일만 10초 테스트하려면 스크립트 생성부터 다시 돌리지 말고 같은 `JOB_ID`로 렌더 단계만 실행합니다.

```bash
cd ~/brain50/prod
export JOB_ID=이미_존재하는_JOB_ID
source ./config.sh

# 중앙 노란 자막 프리셋으로 10초 테스트
./sh/2_render.sh --style center-yellow 10

# 화면 정중앙 기준에서 120px 위로 올려 10초 테스트
./sh/2_render.sh --style center-yellow --offset-y -120 10

# 중앙 흰 자막 프리셋으로 10초 테스트
./sh/2_render.sh --style center-white 10

# 프리셋을 기준으로 폰트/여백만 덮어쓰기
./sh/2_render.sh --style center-yellow --font-size 72 --margin-v 70 10
```

프리셋 이름의 `center`는 최종 1080×1920 쇼츠 화면 전체 기준 중앙 `(540, 960)`을 의미합니다. `center-*` 프리셋은 렌더 직전에 `subs.srt`를 `subs_styled.ass`로 변환하고 `\pos(540,960)` 기준 위치 태그를 넣어 화면 전체 중앙을 고정합니다. `--offset-x`, `--offset-y`는 이 중앙 기준 보정값이며, 예를 들어 `--offset-y -120`은 화면 중앙보다 120px 위입니다.

단, `--font-size`, `--margin-v`, `--margin-h`를 CLI에서 넘기면 프리셋 값보다 우선 적용됩니다. 위치는 `--offset-x`, `--offset-y` 또는 프리셋 파일의 `OffsetX`, `OffsetY`로 조정하는 것을 권장합니다.

프리셋 값은 `caption_styles.yaml`에서 조정합니다. 기본 `CAPTION_STYLE_FILE`은 현재 환경 디렉터리의 `caption_styles.yaml`이며, 필요하면 `CAPTION_STYLE_FILE=/path/to/caption_styles.yaml ./sh/2_render.sh 10`처럼 별도 파일을 지정할 수 있습니다.

상하 검정 safe-zone 프레임을 두고 중앙 B-roll 영역만 사용하려면 `framed` 모드를 사용합니다. 캡션은 `subs_styled.ass`로 최종 캔버스 위에 별도 적용되므로 B-roll 프레임 배치와 독립적으로 유지됩니다.

프레임은 상단/하단 설정 파일로 분리되어 있습니다.

- 상단 여백 프리셋: `frame_top_styles.yaml`
- 하단 여백 프리셋: `frame_bottom_styles.yaml`
- 높이는 최종 1080×1920 캔버스 전체 높이 기준 비율(`height_pct`)로 지정하고, 렌더 직전에 px로 계산됩니다.
- 상단은 대제목(`title`)과 소제목(`subtitle`) 2줄을 지원하며, 여백 내 상하 5px 마진 기준으로 자동 크기를 계산합니다.
- 하단은 상단 끝에서 10px 떨어진 위치에 채널명(`channel_name`, 기본 `브레인피프티`)을 표시합니다.

```bash
# 상단/하단 default 프리셋 + 중앙 B-roll cover
./sh/2_render.sh --frame-mode framed --broll-fit cover --style center-yellow 10

# 상단 brain50 프리셋 + 하단 default 프리셋
./sh/2_render.sh --frame-mode framed --frame-top-preset brain50 --frame-bottom-preset default --style center-yellow 10

# 전체 높이 기준 비율 override
./sh/2_render.sh --frame-mode framed --frame-top-pct 14 --frame-bottom-pct 18 --style center-yellow 10

# 원본 B-roll이 잘리지 않도록 중앙 영역 안에 contain
./sh/2_render.sh --frame-mode framed --broll-fit contain --style center-yellow 10

# 원본은 보존하고 남는 영역은 블러 배경으로 채우는 fallback
./sh/2_render.sh --frame-mode framed --broll-fit blur-contain --style center-yellow 10

# 상단 대제목/소제목, 하단 채널명 override
./sh/2_render.sh --frame-mode framed --top-title "브레인피프티" --top-subtitle "오늘의 뇌건강" --bottom-channel-name "브레인피프티" --style center-yellow 10
```

프레임 텍스트는 FFmpeg `drawtext`로 렌더링되며, 캡션 ASS 프리셋의 `FontName`과 별도입니다. 한글이 `□□□`처럼 보이면 서버에 해당 한글 폰트가 없거나 `drawtext`가 기본 라틴 폰트를 선택한 상태입니다. `fc-match "Noto Sans CJK KR"`로 실제 매칭되는 폰트를 확인하고, 필요하면 상단/하단 프리셋의 `font_file` 또는 `channel_font_file`에 `.ttf/.otf` 파일을 직접 지정하세요.

`run.sh` 전체 실행에서는 주제를 렌더 길이로 오인하지 않도록 `2_render.sh`에 별도 인자를 전달하지 않습니다.

## 단계별 생성과 수동 보정

`1_generate.sh`는 여전히 TTS, caption, B-roll을 순서대로 실행하는 통합 wrapper입니다. 다만 발음이나 자막 보정이 필요할 때는 아래 개별 스크립트를 같은 `JOB_ID`로 실행할 수 있습니다.

```bash
cd ~/brain50/dev
export JOB_ID=test_job_001
source ./config.sh

# 1) script.txt를 읽어 voice.wav 생성
./sh/1_tts.sh

# script.txt를 수동 수정한 뒤 TTS만 다시 생성하려면 같은 명령을 다시 실행합니다.
# 기존 voice.wav는 data/backups/{JOB_ID}/{TIMESTAMP}/tts/ 아래로 이동합니다.

# 2) 수정된 script.txt와 voice.wav를 읽어 subs.srt, scenes_timed.json 생성
./sh/1_caption.sh

# subs.srt를 수동 수정한 뒤 바로 렌더링하려면 caption/broll을 다시 돌리지 않고 2_render.sh로 넘어갑니다.

# 3) scenes_timed.json을 읽어 broll.mp4 생성
./sh/1_broll.sh

# 4) 현재 WORK_DIR의 voice.wav, subs.srt, broll.mp4를 읽어 렌더링
./sh/2_render.sh
```

운영 환경도 같은 방식입니다.

```bash
cd ~/brain50/prod
export JOB_ID=prod_20250621_001
source ./config.sh
./sh/1_tts.sh
./sh/1_caption.sh
./sh/1_broll.sh
./sh/2_render.sh
```

각 단계는 필요한 입력 파일을 `data/work/{JOB_ID}/`에서 읽습니다. 따라서 `script.txt`, `subs.srt`, `scenes_timed.json`을 사람이 수정한 뒤 다음 단계만 이어서 실행할 수 있습니다. 개별 단계 재실행 시 해당 단계가 생성하는 파일만 백업하고, 다른 단계의 수동 수정 파일은 건드리지 않습니다.

## 주제 입력 옵션

텔레그램 명령 연동을 염두에 두고 `0_script.sh`는 두 가지 방식으로 사용할 수 있습니다.

옵션 1: 아이디어를 그대로 대본화합니다.

```bash
cd ~/brain50/dev
export JOB_ID=idea_001
source ./config.sh
./sh/0_script.sh "오메가3가 정말 뇌에 좋을까?"
```

옵션 2: 특정 단어로 Google/YouTube 기반 후보를 먼저 확인한 뒤 선택합니다.

```bash
cd ~/brain50/dev
export JOB_ID=trend_omega3
source ./config.sh
./sh/0_script.sh --trend "오메가3"
```

후보는 `data/work/{JOB_ID}/trend_candidates.json`에 저장되고 터미널에도 1번부터 출력됩니다. 선택한 번호로 실제 대본을 생성합니다.

```bash
./sh/0_script.sh --trend-choice 1
```

`--trend-choice`로 생성한 경우 선택 키워드와 후보 목록은 `video_meta.json`의 `trend_context`에 함께 저장됩니다.
