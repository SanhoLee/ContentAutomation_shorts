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
