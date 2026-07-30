# 🧠 Brain50 Shorts 자동화 파이프라인

> **50대 이후 뇌 건강 정보를 전달하는 YouTube Shorts 콘텐츠를 AI로 자동 제작합니다.**  
> AWS Lightsail에서 동작하며, Telegram으로 단계별 승인·개입이 가능한 하이브리드 자동화 시스템입니다.

---

## 목차

- [파이프라인 전체 흐름](#파이프라인-전체-흐름)
- [디렉토리 구조](#디렉토리-구조)
- [스크립트 모듈 상세](#스크립트-모듈-상세)
- [주요 개선 사항](#주요-개선-사항)
- [사용법](#사용법)
- [피드백 루프](#피드백-루프)
- [환경 변수 목록](#환경-변수-목록)
- [콘텐츠 전략 원칙](#콘텐츠-전략-원칙)

---

## 파이프라인 전체 흐름

```
주제 입력
    │
    ├─ 직접 입력   "치매 초기증상과 건망증 차이"
    ├─ topic.json  구조화된 키워드·전략 파일
    └─ 트렌드 모드  Google/YouTube 자동 후보 수집
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  0_script.py  ─  대본 생성 (2단계)                      │
│                                                         │
│  Stage 1 [Haiku, 빠름·저렴]  plan_strategy()           │
│    main_keyword, 제목, hook_type, core_message 확정     │
│    → strategy.json 저장                                 │
│                                                         │
│  Stage 2 [Sonnet, 품질 집중]  build_prompt()           │
│ PubMed + web_search + YouTube 채널 상대성과 인사이트     │
│    감정 여정 구조로 대본 작성                            │
│    → script.txt / scenes.json / video_meta.json        │
└─────────────────────────────────────────────────────────┘
    │
    ▼
┌──────────────────────────────┐
│  1_tts.py  ─  TTS 음성 생성  │
│  Supertonic 한국어 TTS        │
│  ATEMPO 1.15 (속도 조절)      │
│  → voice.wav                 │
└──────────────────────────────┘
    │
    ▼
┌──────────────────────────────────────────────────────────┐
│  2_caption.py  ─  자막 생성 (스크립트 텍스트 기반)        │
│                                                          │
│  ① script.txt → 한국어 문법 기반 라인 분할               │
│     조사·어미가 앞 단어와 절대 분리되지 않음              │
│  ② voice.wav + initial_prompt → whisper 타임스탬프 추출  │
│  ③ 문자수 비율로 라인 ↔ 타임스탬프 매핑                  │
│  → subs.srt / scenes_timed.json                         │
└──────────────────────────────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────┐
│  3_broll.py  ─  B-roll 수집     │
│  Pexels API, 세로 영상 필터링    │
│  visual_query 기반 자동 검색    │
│  → broll/*.mp4                  │
└─────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────┐
│  render (ffmpeg)                │
│  script + voice + caption + broll│
│  → output.mp4 (1080×1920)      │
└─────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────┐
│  4_upload.py  ─  YouTube 업로드 │
│  Google OAuth, 제목·설명·태그   │
│  → YouTube Shorts 게시          │
└─────────────────────────────────┘
    │
    ▼
┌─────────────────────────────────────────────────────────┐
│  6_youtube_feedback.py  ─  API 실데이터 피드백             │
│                                                         │
│  YouTube Data/Analytics → 채널 상대분포 정규화             │
│  → 주제 중복·성과 방향 → 다음 Stage 1·2에 자동 반영       │
└─────────────────────────────────────────────────────────┘
```

### 실행 모드 — 사람이 몇 번 개입하는가

파이프라인 단계 순서는 `dev/src/common/pipeline_flow.py`의 `STAGES` 한 곳에만
정의되어 있고, 실행 모드는 **어떤 게이트에서 멈추는가**로만 구분됩니다.

| 모드 | 명령 | 사람 개입 | 용도 |
|---|---|---|---|
| `full_gate` | `/run` | 6회 (단계마다) | 세밀하게 손보며 만들 때 |
| `review` | `/run_review` | **2회** (대본 검수 → 최종 승인) | 평소 제작 |
| `auto` | `/run_auto`, `run_goal.sh` | 0회 | 무인 실행 |

```
/run_review "치매 초기증상과 건망증 차이"
  → 대본 생성
  → ★ 게이트1 · 대본 검수     [주제 선정 근거 + 논문 목록 + 검증 결과 + 대본 전문]
  → TTS → 자막 → B-roll → 렌더링   (무인 통과, 단계마다 자동 점검)
  → ★ 게이트2 · 최종 승인     [완성 영상 + 제목/설명 한 화면]
  → 업로드
```

게이트1에서 `이후 자동 업로드`를 누르면 게이트2 없이 끝까지 진행하고,
`/run` 진행 중 `검수 후 최종 컨펌`을 누르면 그 자리에서 2게이트 모드로 전환됩니다.

### 단계 자동 점검 (`stage_guard.py`)

무인 통과 구간에서는 사람이 보지 않으므로, 각 단계 산출물을 결정론적으로 검사합니다.
Claude 호출 없이 파일만 읽으므로 비용은 0입니다.

- 음성 길이가 목표 대비 허용 범위 안인가
- 자막 큐가 있고 타임스탬프가 음성 길이를 넘지 않는가
- B-roll 실패 씬 비율이 허용치 이하인가
- 렌더 결과가 존재하고 길이가 음성과 맞는가

점검에 실패하면 **해당 단계만 1회** 재실행하고(B-roll은 실패 씬만 재검색하는
`1b_retry_broll.sh`), 그래도 실패하면 중단하고 사유와 함께 사람을 호출합니다.
무한 재시도는 하지 않습니다.

### 봇 없이 실행 / 재개 (`run_pipeline.py`)

잡 상태는 `data/work/{JOB_ID}/job_state.json`에 저장되므로, 봇·CLI·cron이 같은
잡을 이어받을 수 있습니다. 스케줄 실행의 전제입니다.

```bash
python3 dev/src/common/run_pipeline.py --job-id J advance --mode review --topic "..."
python3 dev/src/common/run_pipeline.py --job-id J approve   # 게이트 승인
python3 dev/src/common/run_pipeline.py --job-id J advance   # 이어서 진행
python3 dev/src/common/run_pipeline.py --job-id J status
```

종료 코드: `0` 완료 / `10` 게이트 대기(정상) / `1` 중단.

---

## 디렉토리 구조

```
brain50/
├── dev/                        # 개발 환경
│   ├── src/                    # Python 핵심 스크립트
│   │   ├── 0_script.py         # 대본 생성 (2단계 Claude)
│   │   ├── 1_tts.py            # TTS 음성 생성
│   │   ├── 2_caption.py        # 자막 생성 (스크립트 기반)
│   │   ├── 3_broll.py          # B-roll 수집 (Pexels)
│   │   ├── 4_upload.py         # YouTube 업로드
│   │   ├── 6_youtube_feedback.py # YouTube API 성과 피드백
│   │   └── telegram_bot.py     # Telegram 승인 봇
│   ├── sh/                     # Shell wrapper
│   └── data/
│       ├── youtube_feedback.db # YouTube API 성과 SQLite DB
│       └── work/{JOB_ID}/      # 실행별 작업 폴더
│           ├── strategy.json   # Stage 1 전략 결과
│           ├── youtube_guidance.json # 채널 실데이터 분석
│           ├── script.txt      # 생성된 대본 (TTS 입력)
│           ├── scenes.json     # 장면별 텍스트 + visual_query
│           ├── video_meta.json # 제목·훅유형·해시태그 등
│           ├── voice.wav       # TTS 음성
│           ├── subs.srt        # 자막 파일
│           └── scenes_timed.json # 장면별 타임스탬프
├── prod/                       # 운영 환경 (동일 구조)
├── deploy/
│   ├── systemd/                # systemd 서비스 파일
│   └── lightsail/              # 서버 관리 스크립트
├── docs/usage/                 # 상세 사용 가이드
│   ├── basic-usage.md
│   ├── environment.md
│   ├── telegram-bot.md
│   └── with-job-id.md
└── data/
    └── assets/                 # BGM, 공유 자원
```

---

## 스크립트 모듈 상세

### `0_script.py` — 대본 생성 (2단계 Claude)

Claude API를 **두 번 호출**해 전략과 대본을 분리 생성합니다.

**Stage 1 — 전략 수립 (`claude-haiku`, 빠름·저렴)**

주제를 받아 검색 최적화 요소를 먼저 확정합니다.

| 출력 항목 | 설명 |
|-----------|------|
| `main_keyword` | YouTube 검색 핵심 키워드 (12자 이내) |
| `title` | 제목 공식 중 하나. 앞 15자 이내에 main_keyword 포함 |
| `hook_type` | 두려움형 / 반전형 / 숫자충격형 / 공감형 |
| `core_message` | 시청자가 가져갈 딱 한 문장 (30자 이내) |
| `search_intent` | 이 키워드를 검색하는 사람의 상황 |
| `cta_next` | 다음 영상 예고 주제 |

**제목 검색형 공식 4가지**

```
질문형      "[키워드], 정말 ~일까?"
비교형      "[A]와 [B] 차이"
체크리스트형 "[대상]이 ~할 때 보는 N가지"
생활습관형  "[습관]이 뇌에 미치는 영향"
```

**Stage 2 — 대본 작성 (`claude-sonnet`, 품질 집중)**

Stage 1의 전략 + PubMed 초록 + web_search 최신 연구 + YouTube 실데이터 인사이트를 결합해
감정 여정 구조로 대본을 작성합니다.

```
감정 곡선:
불안/호기심 → 이해+놀라움 → 납득+안도 → 흥미+몰입 → 자기인식+공감 → 희망+실천의지

[Scene 1]   훅          — main_keyword 첫 문장 강제 포함
[Scene 2-3] 원리        — 연구 수치 최소 3개 포함
[Scene 4-5] 비유·예시   — 일상 언어로 납득·안도
[Scene 6-7] 의외 포인트 — 흥미·몰입 유발
[Scene 8-9] 공감        — 댓글 트리거 문장 포함
[Scene 10]  행동 + 예고 — 실천 팁(a) + 다음 영상(b) + 공유 유도
```

**web_search 보강**

PubMed 번역에 사용한 영어 쿼리를 재활용해 우선 출처에서 최신 연구를 수집합니다.

```
우선 출처: Nature Neuroscience, Neuron, BrainFacts.org, Neuroscience News,
           NIH/NINDS, Harvard Picower Institute, Stanford, UCL 등
```

---

### `2_caption.py` — 자막 생성 (스크립트 텍스트 기반)

**기존 방식의 문제점**: faster-whisper STT 결과를 자막 텍스트로 사용 →  
"기억력이" → "기억력 / 이" 와 같이 조사·어미가 잘못 끊어지는 고질적 문제 발생

**개선된 방식**:

```
자막 텍스트 = script.txt (원천)
타임스탬프  = faster-whisper (타임스탬프 전용)

① script.txt → split_script_to_lines()
   한국어 문법 규칙 기반 라인 분할
   · 문장 끝(습니다/요/다) → 즉시 줄바꿈
   · 조사(을/를/이/가 등) → 앞 단어에 반드시 붙임
   · 최대 16자 초과 시 절 경계에서 줄바꿈

② voice.wav → get_whisper_words(initial_prompt=script)
   타임스탬프만 추출. 인식 텍스트는 버림.

③ align_lines_to_timestamps()
   문자수 비율로 라인 ↔ 타임스탬프 매핑
   → 단어 경계로 스냅 보정
```

조사·어미 끊김 문제가 원천적으로 해결됩니다.

---

### `6_youtube_feedback.py` — YouTube API 실데이터 피드백

YouTube Data API와 Analytics API에서 최근 90일 성과를 가져와 다음 대본의 Stage 1·2에 자동 반영합니다.

```bash
python dev/src/6_youtube_feedback.py sync
python dev/src/6_youtube_feedback.py report --strictness balanced
python dev/src/6_youtube_feedback.py guide "치매 초기증상" --strictness balanced
```

Shorts 피드 초반 몰입 대리지표·평균 시청률·구독 전환율·공유율·좋아요율·댓글률을 채널 내부 백분위로 정규화합니다. 지속률과 구독 전환율의 채널 적응 기준으로 Q1 치트키/Q2 소재 우수/Q3 몰입 우수/Q4 재검토 전략을 만들고 Stage 1·2에 전달합니다. 표본이 적을수록 초기 기준과 중앙값 쪽으로 보정하며, 영상이 쌓일수록 실제 채널 분포가 기준값을 자동 갱신합니다.

판단 강도는 `loose`(느슨함), `balanced`(중간), `strict`(엄격함) 세 단계입니다. 기본 콘텐츠 생성 명령은 최신 동기화를 먼저 시도하고 실패하면 마지막 정상 DB로 계속합니다.

---

## 주요 개선 사항

### 2단계 Claude 호출 분리

| 구분 | 모델 | 역할 |
|------|------|------|
| Stage 1 | `claude-3-5-haiku-latest` | 전략 수립 (빠름·저렴) |
| Stage 2 | `claude-sonnet-4-6` | 대본 작성 (품질 집중) |

Stage 1이 검색 키워드·제목·훅 유형을 먼저 확정하므로,  
Stage 2 Sonnet은 감정 여정과 문장 품질에만 집중합니다.

### 검색 최적화 강제 규칙

```
제목   : main_keyword가 앞 15자 이내에 위치
Scene 1: 첫 문장에 main_keyword 반드시 포함

나쁜 예 → "혹시 이런 경험 있으세요?"
좋은 예 → "치매 초기증상은 단순 건망증과 헷갈리기 쉽습니다."
```

### 조회 휘발성 억제 장치

| 장치 | 적용 위치 |
|------|----------|
| 댓글 트리거 | Scene 8/9 — "여러분은 몇 시간 주무세요? 댓글로 알려주세요." |
| 공유 유도 | Scene 10 끝 — "부모님께 이 영상 공유해드리세요." |
| 다음 영상 예고 | Scene 10(b) — 시리즈 느낌으로 채널 리텐션 연결 |
| 에버그린 키워드 | 시사성 표현 금지, 검색 지속형 주제 우선 |

---

## 사용법

### 기본 실행

```bash
cd ~/brain50/dev

# 직접 주제 입력
python src/0_script.py "치매 초기증상과 건망증 차이"

# web_search 비활성화 (빠른 테스트)
python src/0_script.py "수면 부족과 기억력 저하" --no-web-research

# Stage 1 건너뜀 (strategy.json 재사용)
python src/0_script.py "주제" --skip-strategy
```

### 구조화된 주제 JSON 입력

반복 제작 시 topic.json으로 전략을 미리 정의할 수 있습니다.

```json
{
  "topic": "치매 초기증상과 건망증 차이",
  "main_keyword": "치매 초기증상",
  "sub_keywords": ["건망증 차이", "부모님 치매"],
  "search_intent": "부모님 기억력 변화가 치매인지 걱정하는 50대",
  "hook_type": "비교형",
  "title": "치매 초기증상과 단순 건망증 차이 3가지",
  "search_title_format": "비교형",
  "core_message": "반복성, 생활 영향, 익숙한 일의 실수 여부를 봐야 한다",
  "cta_next": "경도인지장애와 치매의 차이"
}
```

```bash
python src/0_script.py --topic-json topic.json
```

### 트렌드 기반 주제 선택

```bash
# Step 1: 트렌드 후보 수집 (Google/YouTube 자동 조회)
python src/0_script.py --trend "치매"

# Step 2: 후보 목록 확인 후 선택
# 1. 치매 초기증상 (google_suggest, youtube_suggest)
# 2. 치매 예방 음식 (google_suggest)
# 3. 부모님 치매 (youtube_suggest)
python src/0_script.py --trend-choice 1
```

### 단계별 순차 실행

```bash
python src/0_script.py "치매 초기증상"  # 대본 생성
python src/1_tts.py                     # TTS 음성
python src/2_caption.py                 # 자막
python src/3_broll.py                   # B-roll
# render (ffmpeg)
python src/4_upload.py                  # YouTube 업로드
```

---

## 피드백 루프

영상 성과 데이터가 쌓일수록 대본 품질이 자동으로 개선됩니다.

```
[영상 게시]
    ↓
python 0_script.py "다음 주제"
  → YouTube API 최신 성과 자동 동기화
  → 채널 내부 분위수·표본 신뢰도 보정
  → 초반 몰입·지속률·구독 전환·4분면·주제 중복 분석
  → Stage 1 전략과 Stage 2 대본 프롬프트에 반영
```

---

## 환경 변수 목록

`.env` 또는 `config.sh`에서 설정합니다.

### API 키

| 변수 | 설명 |
|------|------|
| `ANTHROPIC_API_KEY` | Claude API 키 (필수) |
| `PEXELS_API_KEY` | Pexels B-roll 검색 키 |
| `TELEGRAM_BOT_TOKEN` | Telegram 봇 토큰 |
| `SLACK_BOT_TOKEN` | Slack Bot User OAuth Token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | Slack Socket Mode App Token (`xapp-...`) |
| `SLACK_CHANNEL_ID` | Slack 봇 허용 채널 ID (권장) |
| `SLACK_ALLOWED_USER_ID` | Slack 봇 허용 사용자 ID (선택) |

### Claude 모델

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `CLAUDE_STRATEGY_MODEL` | `claude-3-5-haiku-latest` | Stage 1 전략 수립 모델 |
| `CLAUDE_STRATEGY_FALLBACK_MODELS` | `claude-3-5-haiku-20241022` | Stage 1 모델이 400 invalid model 응답을 낼 때 순서대로 재시도할 모델 목록(쉼표 구분) |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Stage 2 대본 작성 모델 |
| `MAX_TOKENS` | `1000` | Stage 2 최대 출력 토큰 |

### 영상 길이

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `TARGET_DURATION_SEC` | `60` | 목표 영상 길이(초) |
| `CHARS_PER_SEC` | `4.66` | 초당 한글 문자수 |
| `ATEMPO` | `1.0` | TTS 재생 속도 배율 |

### web_search

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `ENABLE_WEB_RESEARCH` | `true` | web_search 보강 활성화 |
| `WEB_RESEARCH_TIMEOUT` | `60` | web_search 타임아웃(초). 실패 시 재시도하지 않고 PubMed 중심으로 계속 진행 |
| `WEB_RESEARCH_MAX_USES` | `3` | 1회 요청당 web_search 검색 횟수 하드캡 |
| `WEB_RESEARCH_MAX_TOKENS` | `900` | web_search 요약 응답 토큰 상한 |
| `WEB_RESEARCH_MAX_TOOL_TURNS` | `2` | web_search 보조 호출 루프 상한 |

### B-roll 수집

`3_broll.py` / `broll_policy.py`가 Pexels 결과를 고를 때 쓰는 값입니다. 클립은 **항상 원본 속도로 재생**하며 렌더 단계에서 속도를 조작하지 않습니다. 화면의 생동감은 소스 영상 자체에서 나와야 하고, 느린 영상을 빠르게 돌리면 자연스러운 움직임이 아니라 효과처럼 보이기 때문입니다. 대신 슬로우모션·타임랩스로 촬영된 클립과 정적인 롱테이크를 선택 단계에서 걸러냅니다.

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `BROLL_SLOW_MOTION_PENALTY` | `80` | Pexels가 슬로우모션·타임랩스로 표기한 클립에 대한 감점. 이미 원본이 시간 왜곡된 영상이라 후처리로 되돌릴 수 없음 |
| `BROLL_IDEAL_SOURCE_DURATION` | `12` | 이 길이 이하의 원본 클립에 보너스. 짧은 클립일수록 실제 동작이 담겨 있음 |
| `BROLL_LONG_SOURCE_SECONDS` | `25` | 이보다 긴 원본 클립에 감점(최대 20점). 대부분 정적인 배경 영상 |
| `BROLL_HISTORY_LIMIT` | `300` | 최근 사용한 클립 ID를 기억할 개수. `{data}/broll_usage.json`에 저장 |
| `BROLL_CROSS_JOB_PENALTY` | `55` | 이전 job에서 이미 쓴 클립에 대한 감점. 하드 차단이 아니라 감점이므로 검색 결과가 빈약해도 렌더는 계속 진행됨 |
| `BROLL_CONTENT_ASPECT` | `1080/1300` | 크롭 유지율 계산 기준 화면비 |
| `BROLL_PORTRAIT_TARGET_RATIO` | `0.60` | 세로 클립 목표 비율 |
| `BROLL_MAX_ORIENTATION_STREAK` | `2` | 같은 방향 클립이 연속될 수 있는 최대 횟수 |

### 자막

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `WHISPER_MODEL` | `small` | faster-whisper 모델 크기 |
| `CAPTION_MAX_CHARS` | `16` | 자막 한 라인 최대 글자수 |
| `CAPTION_MIN_CHARS` | `6` | 자막 한 라인 최소 글자수 |
| `CAPTION_OFFSET_SEC` | `-0.15` | 생성된 SRT 타임스탬프 전체 보정값. 음성보다 자막이 늦으면 음수로 앞당김 |
| `CAPTION_FONT_SIZE` | `62` | 렌더 스크립트 기본 자막 폰트 크기 (ASS 1080×1920 좌표 기준) |
| `CAPTION_MARGIN_V` | `60` | 렌더 스크립트 기본 자막 수직 위치 |
| `CAPTION_MARGIN_H` | 환경별 기본값 | 렌더 스크립트 기본 자막 좌우 여백 |
| `CAPTION_STYLE` | `default` | `caption_styles.yaml`에서 선택할 자막 스타일 프리셋 |
| `CAPTION_STYLE_FILE` | `{dev|prod}/caption_styles.yaml` | 사용자 조정 가능한 자막 스타일 프리셋 파일 |
| `CAPTION_OFFSET_X` / `CAPTION_OFFSET_Y` | 프리셋 값 | 중앙 위치 프리셋의 화면 중앙 기준 좌우/상하 보정값 |
| `FRAME_MODE` | `full` | 최종 렌더 프레임 모드. `full`은 기존 전체 화면, `framed`는 상하 검정 safe-zone 프레임 |
| `FRAME_TOP_STYLE_FILE` / `FRAME_BOTTOM_STYLE_FILE` | `{dev|prod}/frame_*_styles.yaml` | 상단/하단 safe-zone 프레임 프리셋 파일 |
| `FRAME_TOP_PRESET` / `FRAME_BOTTOM_PRESET` | `default` | 상단/하단 safe-zone 프레임 프리셋 이름 |
| `FRAME_TOP_MARGIN_PCT` | 프리셋 값 | 헤더 높이를 유지하면서 주제목 위 여백을 조정하는 비율. 값이 클수록 제목 묶음이 아래로 이동 |
| `BROLL_FIT_MODE` | `cover` | 프레임 내부 B-roll 배치 방식. `cover`, `contain`, `blur-contain` |
| `TELEGRAM_DEFAULT_CAPTION_FONT_SIZE` | `62` | 텔레그램 실행 기본 자막 폰트 크기 |
| `TELEGRAM_DEFAULT_CAPTION_MARGIN_V` | `60` | 텔레그램 실행 기본 자막 수직 위치 |
| `TELEGRAM_DEFAULT_CAPTION_STYLE` | `default` | 텔레그램 실행 기본 자막 스타일 프리셋 |
| `TELEGRAM_DEFAULT_WEB_RESEARCH` | `true` | 텔레그램 실행 기본 web_search 사용 여부 |

### 프레임 헤더 자동 생성

스크립트 생성 단계(`0_script.py`)는 상단 검정 safe-zone에 들어갈 2줄 훅 `frame_header`를 함께 생성합니다. 이 문구는 원본 주제어를 그대로 쓰지 않고, 전체 대본 맥락을 압축한 대제목/소제목으로 설계됩니다. 생성된 값은 `data/work/{JOB_ID}/video_meta.json`과 `data/work/{JOB_ID}/frame_header.json`에 저장되며, `2_render.sh --frame-mode framed` 실행 시 `--top-title`/`--top-subtitle`이 없으면 자동으로 적용됩니다.

상단 프레임은 `title_*`/`subtitle_*`, 하단 프레임은 `channel`, `font`, `font_style`, `color`, `size`, `top_margin_px`처럼 역할이 바로 드러나는 키를 사용합니다. 하단 `font_style: Bold`는 렌더 시 실제 Bold 폰트 선택에 반영됩니다. 자세한 예시는 `docs/usage/environment.md`를 참고하세요.

```bash
# 자동 생성된 frame_header를 상단 프레임에 적용
./sh/2_render.sh --frame-mode framed --style center-yellow 10

# 자동 생성값 대신 수동 문구를 강제
./sh/2_render.sh --frame-mode framed --top-title "기억력경고" --top-subtitle "오늘의뇌건강" --style center-yellow 10
```

### YouTube 실데이터 피드백

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `YOUTUBE_FEEDBACK_TOKEN` | 없음 | 읽기 전용 OAuth 토큰 JSON 경로 |
| `YOUTUBE_FEEDBACK_DB` | `dev/data/youtube_feedback.db` | API 성과 SQLite DB 경로 |
| `YOUTUBE_FEEDBACK_STRICTNESS` | `balanced` | `loose` / `balanced` / `strict` |
| `YOUTUBE_FEEDBACK_AUTO_SYNC` | `true` | 콘텐츠 생성 전 최신 API 동기화 여부 |

---

## 콘텐츠 전략 원칙

### 채널 목표

> 바이럴 쇼츠가 아닌, **검색에 계속 걸리는 50대 이후 뇌 건강 쇼츠 라이브러리** 구축

### 콘텐츠 유형 비율

| 유형 | 비율 | 목적 |
|------|------|------|
| 에버그린 검색형 | 60% | 장기 검색 유입 |
| 최신 연구/뉴스형 | 20% | 트렌드 탑승 |
| 부모님 관찰형 | 20% | 공감과 저장 유도 |

### 4대 콘텐츠 필라

1. **수면 & 뇌 건강** — 채널 진입 관문 (공감도 최高)
2. **치매 예방** — 최高 광고 단가
3. **뇌 영양** — 스폰서십 연결
4. **뇌 훈련** — 어필리에이트 수익

### 우선 제작 키워드 TOP 10

```
1군 (즉시 제작)
  치매 초기증상 / 치매 예방 / 기억력 저하 / 부모님 치매 / 경도인지장애

2군 (생활습관 에버그린)
  치매 예방 운동 / 수면 부족 치매 / 고혈압 치매 / 난청 치매 / 시력 저하 치매
```

### 핵심 전달 가치

> **"연구실의 언어를 부모님의 일상 언어로 번역하는 것"**  
> 정보 전달이 아니라 **행동 변화**와 **근거 있는 희망**을 전달합니다.

---

## 관련 문서

| 문서 | 내용 |
|------|------|
| [docs/usage/basic-usage.md](docs/usage/basic-usage.md) | 기본 실행 가이드 |
| [docs/usage/telegram-bot.md](docs/usage/telegram-bot.md) | Telegram 봇 상세 |
| [docs/usage/slack-bot.md](docs/usage/slack-bot.md) | Slack 봇 설치·권한·운영 |
| [docs/usage/environment.md](docs/usage/environment.md) | 환경 설정 |
| [docs/usage/with-job-id.md](docs/usage/with-job-id.md) | JOB_ID 활용법 |
| [HANDOFF.md](HANDOFF.md) | 개발 히스토리 |
| [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) | 프로젝트 컨텍스트 |
