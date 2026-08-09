# 알려진 이슈 및 리스크 목록

최종 업데이트: 2026-08-09
기준 브랜치: `main`

## 활성 / 모니터링 항목

### 1. Stage 0 런타임 설정 분산 재발 위험

`total_chars`, `ENABLE_WEB_RESEARCH` 누락 사고 이후 `dev/src/common/script_runtime.py`로 env 기본값을 중앙화함. `0_script.py`에 새 env 파싱을 직접 추가하지 말고 `script_runtime.py`에 추가할 것 (전에도 재발한 문제, `CLAUDE.md` 참고).

### 2. 자막 타이밍 튜닝 여지

`2_caption.py`는 Whisper 순차 타임스탬프 + 한국어 문법 기반 줄바꿈(`korean_grammar.py`) + `CAPTION_OFFSET_SEC=-0.15` 사용 중. 자막이 늦으면 `-0.20` 쪽으로, 빠르면 `0` 쪽으로 조정. 렌더 마진/폰트 설정이 아니라 체감 튜닝 값.

### 3. systemd 환경 TTS CLI 경로

`config.sh`가 PATH에 `$HOME/.local/bin:/usr/local/bin` 추가로 완화됨. 서버 권장 설정: `export TTS_BIN=/home/ubuntu/.local/bin/supertonic`

### 4. PubMed 검색 결과 없음

`evidence_probe.py`가 full → narrowed → core → category 순으로 단계적 검색. 전부 실패 시 `ALLOW_NO_PUBMED=1`일 때만 생성 계속 (더 이상 하드코딩된 `True` 아님). `pubmed_status.json`의 `ladder_rung` 분포 확인 — `category` 비중이 높으면 원본 쿼리가 너무 좁다는 신호(Seed Interpreter 쿼리 품질 문제).

### 5. YouTube 업로드 최종 단계

최근 로컬 테스트에서 많이 검증되지 않음. 업로드 동작 변경 전 `dev/src/youtube/4_upload.py` 확인 필요.

### 6. `hook_open_loop` / 제목-해시태그는 경고일 뿐, 게이트 아님 (2026-08-01)

`validate_script()`에 `missing_hook_open_loop`, `hooky_title_hashtag`를 경고로만 추가 (모델이 필드 누락해도 무인 실행이 멈추면 안 됨 — production-continuity 원칙). `script_quality.json`의 `missing_hook_open_loop` 빈도가 30% 이상이면 프롬프트에 더 확실한 예시 필요 (게이트 강화 아님).

### 7. `classify_api_error`가 `RefreshError`를 오분류, OAuth 재인증 필요성이 며칠간 숨겨짐 (코드는 수정됨, 2026-08-01)

`load_credentials()`의 `creds.refresh()`가 던지는 `RefreshError`에 `.resp.status`가 없어 항상 일반 `"API/동기화 실패"`로 빠짐 (`"인증/권한 실패"`가 맞음). 2026-07-26부터 16회 연속 실패(run_id 23-38), 계획 작업은 원인 불명인 채 `decision=manual_review`로 빠짐.

수정: `classify_api_error`가 `RefreshError`를 명시적으로 체크. `0_topic_plan.py`가 `feedback.latest_sync_error()`로 Slack 메시지에 실제 원인 노출. 진단용 서브커맨드 추가: `check-auth`(쿼터 미사용), `reauth`(헤드리스 SSH 포트포워딩으로 강제 재인증). 런북: `docs/usage/youtube-feedback.md` 9장.

근본 원인 추정: Google Cloud OAuth 동의 화면이 "테스트" 상태라 refresh token이 7일 제한 — Google Cloud Console에서 "프로덕션으로 게시" 필요 (코드로 해결 불가, 운영 조치). 동기화 상태 점검용 예약 작업(cron/systemd timer)은 아직 없음 — 의도적 범위 제한 (`CLAUDE.md` North star 참고).

### 8. `over_target_length`를 경고로 강등 (2026-08-02)

`TARGET_DURATION_SEC` 80→55초 축소(`7c30586`) 후에도 Stage 1이 요구하는 `required_beats` 개수는 줄지 않아, 하드 캡(`MAX_SCRIPT_LENGTH_RATIO=1.40`) 초과로 작업 전체가 죽는 사고 발생(job `goal_20260802_025044_005235_f61bea61`).

수정: `over_target_length`를 `errors`에서 `warnings`로 이동(#6과 같은 production-continuity 원칙). 실제 안전망은 `stage_guard.py`가 재는 실제 TTS 오디오 길이(0.5x~1.8x 허용, 글자수 캡보다 훨씬 관대). 기계적 트리밍은 추가하지 않음(제품 결정) — 압축 1회 실행 후 여전히 초과하면 그대로 진행.

**모니터링**: `script_quality.json`의 `over_target_length` 빈도가 30% 이상이면 Stage 1 프롬프트(`required_beats`/hedging 문구)를 55초 예산에 맞게 줄여야 함.

### 9. `data/work/`가 지워지면 story_type 믹스 리셋 (2026-08-02)

`story_types.recent_story_types()`는 `data/work/*/strategy.json`에서 최근 장르 히스토리를 읽는데, 이 디렉토리는 gitignore 대상이라 디스크 정리/새 컨테이너/서버 이전 시 히스토리가 날아감. 이후 픽커가 콜드스타트로 `principle_experience`부터 배정, 설정된 35/30/25/10 분포로 수렴하는 데 약 10개 작업 소요.

부분 완화: 피드백 DB 백필(`objective_planner._recent_format_types`)이 게시된 영상은 커버하지만, 작업했지만 미게시된 job은 커버 못함.

의도적으로 미수정: 전용 저장소를 추가하는 대신, 작업 디렉토리 삭제가 잦아지면 그때 최근 20개 job의 story_type 분포를 확인.

### 10. Stage 2 story_type 체크에 stage_guard 미적용 (2026-08-02, 설계 의도)

`visual.brief` 누락 시 재시도 1회 옵션이 있었지만 채택 안 함 — 재시도가 Stage 1+2 전체를 다시 돌려 Claude 비용이 두 배가 되기 때문(비용 원칙 위반, #7/#10, `CLAUDE.md` 참고).

대신 `scene_visuals.fill_scene_visuals()`가 장면 텍스트로 `brief`를 결정적으로 백필하고, 경고(`visual_brief_backfilled`, `visual_plan_unavailable`)만 `script_quality.json`에 기록(비용 0). `validate_script()`는 `role_off_sequence`/`role_bookends_missing`만 담당. **모니터링**: `visual_brief_backfilled`가 30% 이상이면 가드가 아니라 `scene_visuals.PLAN_PROMPT`를 손봐야 함.

### 13. 시각 계획을 Stage 2에서 분리 (2026-08-08)

`script.txt`는 `scenes.json`을 `"\n\n".join(scene["text"])`로 펼친 같은 객체다. 그런데 승인 게이트에서 본문을 고치면 `script.txt`만 바뀌고 `scenes.json`의 `text`/`role`/`visual`/`visual_query`는 수정 전 상태로 남아, TTS·자막은 새 본문을, B-roll·X 스레드는 지워진 문장을 따라갔다(job `review_20260808_134830_992877_c43bc94c`).

수정: 시각 계획을 Stage 2에서 떼어내 승인 게이트 **뒤**의 별도 스테이지 `scene_visuals`로 옮김(`pipeline_flow.STAGES`에서 `script` 다음, `x_thread` 앞). 이 스테이지가 확정된 `script.txt` 기준으로 씬 텍스트를 재동기화하고 Haiku 1회로 `visual`/`visual_query`를 계획한 뒤 `content_package.json`을 다시 만든다.

- **비용**: 작업당 Haiku 1회 ≈ $0.0075 (실측). Stage 1+2 재실행이 아니므로 #10 원칙과 충돌하지 않음. 계획 호출만 끄려면 `SCENE_VISUALS_PLAN=0` (텍스트 재동기화와 결정론적 채우기는 계속 동작).
- **주의**: 슬랙으로 붙여넣은 수정본은 빈 줄이 사라져 한 줄에 한 씬으로 들어온다. `split_script_paragraphs()`가 빈 줄이 아니라 **모든 개행**으로 나누는 이유 — `\n\n`만 쓰면 8개 씬이 1개로 뭉개진다.
- **모니터링**: `script_quality.json`의 `visual_plan_source`가 `fallback`인 비율. 계속 fallback이면 모든 영상이 같은 기본 검색어(`senior person daily life home`)로 나간다.

### 11. X 토큰에 `media.write` 스코프 없음 — 운영 조치 필요 (2026-08-08)

**확인된 원인.** `data/x_token.json`의 OAuth 2.0 토큰이 `media.write` 없이 발급되어 미디어 업로드가 403으로 실패한다. `api.x.com/2/media/upload`은 `{"detail":"Forbidden"}`만 주지만 `upload.x.com/2/media/upload`은 `{"detail":"You are not permitted to use OAuth2 on this endpoint"}`를 준다 — 스코프 누락의 전형적 증상. 저장된 토큰에는 `scope` 필드 자체가 없다.

이 때문에 job `review_20260808_134830_992877_c43bc94c`은 운영자가 슬랙에 사진을 올렸는데도(14:57 수신 확인) 텍스트만 게시됐다. 당시 `x_poster`가 업로드 실패를 조용히 삼키고 텍스트만 내보냈기 때문에 실패가 드러나지 않았다.

**코드 수정(2026-08-08).**

- 리드 트윗 이미지가 **운영자가 준 것**(`photo_source == "operator"`)이 아니면 게시를 보류(`PhotoPending`, 종료코드 2). 자동 생성 카드만으로는 나가지 않는다.
- 사진 업로드가 실패하면 트윗을 하나도 올리지 않고 중단 — 트윗 1이 한 번 나가면 미디어를 나중에 붙일 수 없기 때문. 재개(`resume_from > 0`)와 `/x_post`(force)는 예외.
- 슬랙에 사진이 도착하면 그 자리에서 보류된 스레드를 게시(`_maybe_post_x_thread`).
- 이미지 없이 지금 내보내려면 `/x_post`.

**남은 운영 조치**: X Developer Portal에서 앱에 `media.write` 포함해 재인증(코드로 해결 불가). 그 전까지 모든 스레드는 사진 대기 상태로 멈추거나 `/x_post`로 텍스트만 나간다.

과거 이력: `074f483`(2026-08-02)의 "sources tweet"용 `LINK_COST_CHARS`는 같은 날 `e6d0d62`에서 트윗 자체와 함께 제거 — `sources_text`는 Slack DM으로만 전송(`_maybe_send_x_sources_dm`).

### 12. Seed anchoring이 자동완성 풀을 얇게 만들 수 있음 (2026-08-07)

위험인자 시드가 앵커링되어 검색됨(예: `고혈압` → `치매 고혈압`, `dev/config/topic_domain.json`의 `seed_anchors`) — 두 단어 쿼리는 한 단어보다 자동완성 결과가 적고, `trend_probe.probe()`는 온도메인 제안이 `MIN_KEPT=3` 미만인 언어 rung을 버림.

완화책: `batch_expand()`가 `off_topic` 앵커 시드를 원본 시드로 1회 재시도, 결과는 `no_domain_anchor`로 걸러짐 → 실패 모드는 헛된 suggest 호출이지 잘못된 주제가 아님.

**액션**: 실제 `--refresh` 후 `data/topics/raw/{stamp}_run.json`에서 `anchored_seed_count` 대비 `anchor_fallback_count` 확인. 절반 넘으면 `seed_anchors`를 실제로 사람들이 입력하는 표현으로 바꾸거나 `max_anchor_variants`를 2로 올릴 것(suggest 호출 수 증가, `max_seeds_total`로 제한됨).

### 14. 훅 패턴 어휘 통일, 숫자형/호기심갭형 구체성 규칙 추가 (2026-08-09)

Stage 1/Stage 2 프롬프트, 플래너 enum, 피드백 DB가 각각 다른 훅 라벨 어휘를 썼다(`숫자충격형` vs `질문형` vs `즉각지목형` 등). `hook_types.py`가 6개 패턴(`숫자형`/`호기심갭형`/`반전형`/`지목형`/`경고형`/`폭로형`)으로 통일하고, `normalize()`가 레거시 라벨과 DB에 이미 쌓인 값을 canonical로 변환한다(`objective_planner.validate_planner_output`, `format_hook_repeat` 비교 양쪽 모두 포함).

`validate_script()`에 `hook_number_not_concrete`(숫자형인데 Scene 1에 실수치 없음/뭉뚱그린 표현), `hook_open_loop_not_anchored`(오픈루프가 '이 방법' 같은 지시대명사에 걸림) 경고 2개 추가 — #6/#8/#10과 같은 production-continuity 원칙으로 경고일 뿐 게이트 아님.

**모니터링**: `script_quality.json`의 `hook_number_not_concrete`/`hook_open_loop_not_anchored` 빈도가 30% 이상이면 프롬프트 예시를 더 구체적으로 보강. 숫자형 체크는 `re.search(r"\d")` 기반 휴리스틱이라 "일곱 명 중 하나" 같은 한글 수사는 놓친다 — 오탐 비율이 높으면 한글 수사 테이블 추가 검토.

## 해결됨

재발 시에만 다시 열 것.

- **백그라운드 스레드 상태 저장**: `STATE_LOCK`으로 보호, 임시 파일 후 `os.replace`로 원자적 교체.
- **`/approve` 텍스트 명령에 stage 토큰 없음**: 의도된 동작. 인라인 버튼만 stage 토큰으로 stale 거부.
- **web_search 비용/타임아웃**: 경계 설정됨 (`ENABLE_WEB_RESEARCH=true`, `WEB_RESEARCH_TIMEOUT=60`, `WEB_RESEARCH_MAX_USES=2` 등, `script_runtime.py`). 타임아웃 시 빈 결과로 계속 진행, 중복 비용 방지 위해 자동 재시도 없음.
- **렌더 진행률**: `2_render.sh`가 ffmpeg `-progress`로 `render_progress.txt` 기록, Slack이 25/50/75%에 체크포인트 전송.
- **Claude API 읽기 타임아웃**: `CLAUDE_TIMEOUT` 기본 180초, 429/5xx는 `CLAUDE_HTTP_RETRIES` 내 재시도, 읽기/연결 타임아웃은 중복 비용 방지 위해 재시도 안 함.
- **Slack 파일 편집 UX 제약**: 영구 허용된 방식. `script.txt`/`subs.srt`/`video_meta.json`은 파일 재업로드로 덮어쓰기만 지원.
- **Windows 터미널 인코딩**: 로컬 표시 문제일 뿐 소스 손상 아님. `PYTHONIOENCODING=utf-8` 사용. 실제 버그(`trend_probe.py`의 Google Suggest 요청에 `ie/oe=utf-8` 누락)는 2026-07-31 수정됨.
- **`threshold`는 현재 가중치에 종속적 (2026-08-07)**: `domain_relevance`(30) 추가로 가중치 재조정 후 `threshold`를 60→45로 하향. `dev/config/topic_score_rules.json` 가중치 변경 시 threshold도 재산출 필요 — `tests/test_topic_score.py::ShippedConfigCalibrationTests`가 검증.

## 다음 테스트 세션에서 확인할 버그

- systemd 재시작 급발생 시 welcome/bye 메시지 중복
- SIGKILL/서버 강제 종료 시 bye 메시지 누락
- 서비스 재시작 후 오래된 Slack 버튼은 stage 불일치로 거부되어야 함
- 강제 프로세스 종료 후 busy 플래그가 고착되면 `/status`가 `busy`로 표시됨 — `/cancel`로 해제
- 매우 짧은 렌더 작업은 시작/완료만 표시될 수 있음
- 한국어 문법 세그멘테이션 개편(`213f3f1`) 이후 실제 렌더 결과로 자막 싱크 재검토, 필요 시 `CAPTION_OFFSET_SEC` 조정
- 신규 게시 Shorts의 3.6~8초 이탈(항목 6 참고) — 새 프롬프트로 만든 첫 5개 영상에서 목표는 10%p 이하 이탈; 10~20%p면 Scene 2 규칙 추가 강화, 20%p 초과면 스크립트 구조가 원인이 아니라 오디오/자막 밀도가 원인일 가능성
