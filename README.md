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
│    PubMed 초록 + web_search 최신 연구 + 피드백 인사이트  │
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
│  5_feedback.py  ─  피드백 & 인사이트 (신규)              │
│                                                         │
│  영상 게시 후 평점·YT 지표·키워드 태깅 → SQLite DB        │
│  python 5_feedback.py insights                          │
│  → feedback_insights.json → 다음 대본에 자동 반영        │
└─────────────────────────────────────────────────────────┘
```

### Telegram 승인 워크플로

전체 파이프라인은 Telegram을 통해 단계별 승인/수정/재실행이 가능합니다.

```
/run "치매 초기증상과 건망증 차이"
  → 대본 생성 → [승인 / 수정 / 재실행]
  → TTS 생성  → [승인 / 수정]
  → 자막 생성 → [승인]
  → B-roll    → [승인]
  → 렌더링    → [승인]
  → 업로드    → [승인]
```

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
│   │   ├── 5_feedback.py       # 피드백 & 인사이트 ← 신규
│   │   └── telegram_bot.py     # Telegram 승인 봇
│   ├── sh/                     # Shell wrapper
│   └── data/
│       └── work/{JOB_ID}/      # 실행별 작업 폴더
│           ├── strategy.json   # Stage 1 전략 결과 ← 신규
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
    ├── assets/                 # BGM, 공유 자원
    └── feedback.db             # 피드백 SQLite DB ← 신규
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

Stage 1의 전략 + PubMed 초록 + web_search 최신 연구 + 피드백 인사이트를 결합해
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

### `5_feedback.py` — 피드백 & 인사이트 시스템 *(신규)*

영상 반응 데이터를 SQLite에 누적하고, 인사이트를 다음 대본 생성에 자동 반영합니다.

**명령어**

```bash
# 영상 게시 후 평가 입력 (video_meta.json 자동 읽기)
python 5_feedback.py rate

# 며칠 후 YouTube 조회수·시청률 추가
python 5_feedback.py update [video_key]

# 특정 단어/표현 태깅
python 5_feedback.py tag <video_key> "치매 예방" +1 --ktype topic_word

# 목록 / 통계
python 5_feedback.py list
python 5_feedback.py stats

# 인사이트 생성 → feedback_insights.json → 다음 0_script.py에 자동 반영
python 5_feedback.py insights
```

**DB 스키마**

```sql
videos   -- video_key, topic, title, hook_type
            rating(1-5), yt_views, yt_watch_pct, yt_likes, yt_comments
keywords -- video_key, keyword, ktype, sentiment(+1/0/-1)
```

**누적 효과**

| 누적 영상 수 | 의미 있는 인사이트 |
|------------|-----------------|
| 5개 이상   | 훅 유형별 평점 비교 |
| 10개 이상  | 주제 패턴 신뢰도 향상 |
| 20개 이상  | 키워드 통계 유의미 |

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
python 5_feedback.py rate
  → 평점(1-5), YT 지표, 키워드 태깅 입력
  → feedback.db 누적
    ↓
python 5_feedback.py insights
  → 훅 유형별 성과, 좋은/나쁜 키워드 분석
  → feedback_insights.json 저장
    ↓
python 0_script.py "다음 주제"
  → feedback_insights.json 자동 읽기
  → "반응 좋았던 훅 유형" 등 프롬프트에 반영
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

### Claude 모델

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `CLAUDE_STRATEGY_MODEL` | `claude-3-5-haiku-latest` | Stage 1 전략 수립 모델 |
| `CLAUDE_STRATEGY_FALLBACK_MODELS` | `claude-3-5-haiku-20241022` | Stage 1 모델이 400 invalid model 응답을 낼 때 순서대로 재시도할 모델 목록(쉼표 구분) |
| `CLAUDE_MODEL` | `claude-sonnet-4-6` | Stage 2 대본 작성 모델 |
| `MAX_TOKENS` | `4000` | Stage 2 최대 출력 토큰 |

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
| `WEB_RESEARCH_TIMEOUT` | `120` | web_search 타임아웃(초) |

### 자막

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `WHISPER_MODEL` | `small` | faster-whisper 모델 크기 |
| `CAPTION_MAX_CHARS` | `16` | 자막 한 라인 최대 글자수 |
| `CAPTION_MIN_CHARS` | `6` | 자막 한 라인 최소 글자수 |

### 피드백

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `FEEDBACK_DB` | `~/brain50/data/feedback.db` | SQLite DB 경로 |
| `FEEDBACK_INSIGHTS` | `~/brain50/data/feedback_insights.json` | 인사이트 파일 경로 |

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
| [docs/usage/environment.md](docs/usage/environment.md) | 환경 설정 |
| [docs/usage/with-job-id.md](docs/usage/with-job-id.md) | JOB_ID 활용법 |
| [HANDOFF.md](HANDOFF.md) | 개발 히스토리 |
| [PROJECT_CONTEXT.md](PROJECT_CONTEXT.md) | 프로젝트 컨텍스트 |
