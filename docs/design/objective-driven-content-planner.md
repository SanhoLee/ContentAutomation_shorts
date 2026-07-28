# 목표 지표 기반 Shorts 기획 엔진

이 문서는 `dev`에 구현된 목표 기반 자동 기획 흐름의 운영 계약을 설명한다. 기존 직접 주제 명령은 그대로 유지하며, 목표 기획은 별도 진입점으로 실행한다. `prod`에는 dev 제한 테스트와 사용자 승인 전까지 반영하지 않는다.

## 실행 흐름

```text
목표·선택적 씨드
→ 6시간 TTL 기준 YouTube read-only 동기화
→ D1_APPROX / D7 / D28 / ROLLING_90D 스냅샷
→ 길이·기간·topic family cohort 정규화
→ Haiku Seed Interpreter (계열·주제 표현·근거 관련성 판단)
→ exploit 5 + adjacent 3 + trend 3 + wildcard 1 후보
→ Haiku Planner
→ Python 목표별 점수
→ 상위 3개 Haiku Critic
→ Python Judge
→ topic_plan.json
→ 기존 0_script.py --topic-json
→ TTS / 자막 / B-roll / 렌더 / 비공개 업로드
→ upload_result.json 및 DB video_id 연결
```

Seed Interpreter, Planner, Critic은 정성적 판단·제안·반증만 담당한다. 분위수, 신뢰도, 비용, 페널티, 최종 선택은 Python이 계산한다. AI가 입력에 없는 후보·영상·근거를 참조하거나 허용 enum 외 값을 반환하면 결과를 거부하고 deterministic fallback을 사용한다.

## Seed Interpreter

후보 문자열이 만들어지기 전에 한 번 실행하는 방향 설정 단계다. 임의의 씨드가 들어와도 채널 데이터와 연결되도록, 세 가지만 판단한다.

- `resolved_family`: 씨드를 다룰 주제 계열. 기존 계열이나 `research_categories.json` 카테고리에 맞으면 재사용하고, 없으면 24자 이내 새 계열명을 제안한다. `family_source`는 `existing` / `research_category` / `new`.
- `topics`: mode별 주제(exploit 4 / adjacent 3 / wildcard 2 권장). 각 항목은 조각이 아니라 그대로 영상 제목이 되는 완결된 문장이다. 후보 풀 크기는 mode별 서로 다른 주제 개수로 제한되므로 개수가 곧 탐색 폭이 된다.
- `evidence_relevance`: 채널 상위 영상 각각이 이 씨드와 내용상 관련(`topical`)인지, 형식·훅 패턴 참고용(`pattern_only`)인지 표시.

입력은 Python이 결정론적으로 준비한다. 성과는 이미 계산된 분류 라벨(`strategy_success` / `exposure_luck` / `hidden_success` / `weak_result` / `insufficient_data`)로만 전달하고 원시 수치는 넘기지 않는다. 채널 영상은 제목과 함께 전달하므로 모델이 무관한 근거를 식별할 수 있다.

이 단계가 없으면 `TOPIC_FAMILY_RULES` 문자열 부분일치와 `TOPIC_ANGLE_TEMPLATES` 고정 문구만으로 후보가 만들어진다. 키워드에 없는 씨드는 씨드 단어 자체가 계열이 되고, 보조식품 도메인 문구가 모든 주제에 붙는다. 호출 실패·예산 초과·검증 실패는 모두 이 기계적 경로로 fallback하며 파이프라인을 중단하지 않는다.

주제 개별 문제(길이 범위 이탈, 근거 없는 수치, 기존 제목 복사)는 해당 주제만 `skipped_topics`로 제외하고 나머지를 사용한다. `resolved_family` 누락, 허용 외 `family_source`, 입력에 없는 `ref`, 허용 외 `relevance`는 전체 응답을 거부한다.

## 제목 표현

`"<씨드>: <각도>"` 형태는 조각 템플릿(`TOPIC_ANGLE_TEMPLATES`)을 문자열로 이어 붙이던 흔적이며, 사전에서 용어를 설명하는 말투로 읽힌다. Interpreter가 완결된 제목을 직접 만들므로 이 접두사 규칙은 deterministic fallback 경로에만 남는다. Interpreter가 주제를 제공한 mode 외에, 비어 있는 mode에 접두사 템플릿을 섞어 넣지 않는다. 표현 방식이 섞이면 접두사 스타일이 다시 살아나기 때문이다.

씨드 단어가 제목에 그대로 나올 필요는 없다. 따라서 Planner 출력에 대한 씨드 단어 포함 검사는 없다. 대신 Planner는 `topic`을 바꿀 수 없다. 확정된 후보 주제를 그대로 유지하므로 씨드 범위를 벗어나는 것이 구조적으로 불가능하고, 중복 검사를 우회한 재작성도 생기지 않는다. Planner가 정하는 것은 형식·훅·정성 평가다.

말투 기준은 두 가지다. 둘 다 이미 수집하는 데이터이며 새 수집 경로를 만들지 않는다.

- 채널 자신의 기존 제목(`videos.title`): 이 채널이 실제로 쓰는 어투.
- Google/YouTube 자동완성 표현(`trend_observations.topic`): 이용자가 직접 입력한 말. `0_topic_plan.py:collect_trend_signals`가 이미 씨드마다 수집해 저장하고 있었지만 Interpreter에는 전달되지 않았다.

자동완성 표현은 제목에 그대로 옮기지 않는다. 단어 선택과 궁금해하는 지점만 참고하며, `"<씨드> 뜻"` / `"<씨드> 유의어"`처럼 용어 해설을 찾는 검색어는 제목 근거로 쓰지 않도록 프롬프트에서 제외한다.

**댓글 본문은 동기화하지 않는다.** 따라서 별도 커뮤니티 말뭉치도 두지 않으며, 위 두 소스가 유일한 어투 근거다.

## 기획과 카피의 분리

`topic_plan.json`은 기획 계약만 담는다. `topic`, `objective`, `content_design`, `planning`뿐이고 카피 필드(`main_keyword`, `title`, `thumbnail_text`, `frame_header`, `core_message`, ...)는 담지 않는다.

예전에는 이 단계가 카피 필드를 자체적으로 만들어 넣었는데, `0_script.py`는 `--topic-json`에 `main_keyword`가 있으면 Stage 1을 건너뛴다(`0_script.py`의 Stage 1 분기). 그래서 자리를 채우려고 만든 값이 그대로 렌더까지 갔다. 주제 문장 전체가 검색 키워드(`main_keyword`)로 들어가고, 상단 헤더는 9자/18자 슬라이스로 단어 중간에서 끊겼다.

이제 카피는 Stage 1(`plan_strategy`)이 만든다. Stage 1은 이 일을 위해 만들어진 단계이고 PubMed·web research 문맥까지 갖고 있으며, `evidence_brief`를 생성해 Stage 2가 원문 대신 요약 근거를 쓰게 한다(`stage2_research_context`는 `strategy_source == "claude"`일 때만 이 경로를 쓴다).

기획 단계의 결정은 두 방향으로 보존한다.

- `content_design`(형식·훅·감정 곡선·계열·각도)을 `design_constraint_hint`로 Stage 1 프롬프트에 제약으로 넣어, Stage 1이 형식을 다시 정하지 않고 카피만 쓰게 한다.
- Stage 1 실행 후 `merge_planning_contract`로 `content_design` / `objective` / `planning`과 기획이 고른 `hook_type`을 복원한다. `strategy_source`는 Stage 1의 `claude`를 유지하고, 기획 출처는 `planning_source`에 따로 기록한다.

`4_upload.py`는 실제 렌더·업로드된 카피(`video_meta.json`)와 기획 출처(`topic_plan.json`)를 병합해 DB에 남긴다. 이전에는 `topic_plan.json`을 통째로 우선해 자리 채우기용 `main_keyword`가 `content_features`에 저장됐다.

## CLI

전체 자동 파이프라인:

```bash
cd /home/ubuntu/brain50/dev
./run_goal.sh subscriber_growth
./run_goal.sh subscriber_growth "수면"
./run_goal.sh retention "기억력"
./run_goal.sh balanced
```

기획만 실행:

```bash
source ./config.sh
python3 src/0_topic_plan.py plan \
  --objective subscriber_growth \
  --seed "수면" \
  --output "$WORK_DIR/topic_plan.json"
```

API 없이 deterministic dry-run:

```bash
python3 src/0_topic_plan.py plan \
  --objective retention \
  --seed "기억력" \
  --no-ai --no-sync --no-trends \
  --allow-stale \
  --output "$WORK_DIR/topic_plan.json"
```

상태·보고·주기 감사:

```bash
python3 src/0_topic_plan.py status
python3 src/0_topic_plan.py report --output "$WORK_DIR/goal_report.json"
python3 src/0_topic_plan.py refresh --objective subscriber_growth
```

`--require-runnable`을 사용해도 `manual_review`/`rejected`만으로는 중단하지 않는다. 결정론적 fallback은 항상 후보를 하나 만들어내므로, confidence가 낮거나 Planner/Critic 호출이 실패해도 그 후보로 제작을 계속 진행한다(품질 판단은 이후 성과 데이터로 반영). exit code 2로 중단하는 유일한 경우는 씨드로 만들 수 있는 후보가 하나도 없을 때(`planning.candidate_count == 0`)뿐이다. `run_goal.sh`은 이 옵션을 사용하므로 오래된 YouTube 동기화 데이터(`--allow-stale` 미지정 시)나 후보가 전혀 없는 경우에만 자동 진행을 멈춘다.

## Telegram / Slack

```text
/run_goal subscriber_growth
/run_goal subscriber_growth 수면
/goal_status
/goal_report
```

봇의 `/run_goal`은 기획 후 기존 승인형 스크립트 검수 흐름으로 연결한다. `manual_review` 또는 `rejected`이어도 후보가 하나라도 있으면(`planning.candidate_count > 0`) 그 후보로 제작을 계속 진행하며, 낮은 확신도 상태임을 메시지로만 알린다. 씨드로 만들 수 있는 후보가 전혀 없을 때만 `topic_plan.json`을 보존하고 버튼으로 재기획/홈 이동을 안내한다.

Slack `dev` 홈에서는 명령 입력 없이 `목표 기반 자동 기획` 버튼으로 같은 흐름을 시작할 수 있다. 버튼 UI는 목표 선택, 씨드 없음/직접 입력 선택, 최종 실행 확인의 3단계로 구성한다. 최종 확인 전에는 기존 작업 상태를 유지한다. 이 UI는 dev 제한 원칙에 따라 `prod`에는 아직 반영하지 않는다.

## 목표 프로필

지원 목표:

- `subscriber_growth`: 조회수 대비 순 구독 전환을 중심으로 평가
- `reach`: 도달, 초반 몰입, 다중 Suggest 출현과 새로움을 평가
- `retention`: 평균 시청률, 초반 몰입, replay lift, 길이 cohort 시청률을 평가
- `share_growth`: 공유율, 실천 가능성, 가족 관련성과 신뢰를 평가
- `balanced`: 기존 채널 종합 성과 가중치와 호환

모든 프로필 가중치 합은 1.0이며, 원시 수치 대신 동일 cohort 내 0~1 분위수를 사용한다. 한국어 별칭도 Python API에서 정규화한다.

## 데이터 저장

기존 `dev/data/youtube_feedback.db`를 확장한다. 모든 연결은 다음 설정을 사용한다.

```sql
PRAGMA journal_mode=WAL;
PRAGMA busy_timeout=5000;
PRAGMA foreign_keys=ON;
```

핵심 테이블:

- `objectives`: 목표와 코드 가중치
- `video_jobs`: `job_id`, `plan_id`, `objective_id`, 업로드 `video_id`
- `content_features`: 형식, 훅, 감정 곡선, 시리즈, 탐색 모드
- `performance_snapshots`: 기간별 성과
- `planning_runs`: 전체 후보와 Planner/Critic/Judge 결과
- `strategy_hypotheses`: 지지·반증·confidence·TTL
- `hypothesis_observations`: D28 결과의 중복 반영 방지
- `strategy_audits`: 5/15/30편 리프레시 결과
- `claude_usage`: 호출별 토큰·검색·예상 비용

기존 DB에서 여러 번 연결해도 migration은 중복 실행되지 않으며 기존 데이터를 유지한다.

## 성과 비교와 안전 규칙

cohort는 같은 snapshot window와 길이 bucket을 우선한다. topic family 표본이 5개 이상이면 같은 family를 쓰고, 부족하면 같은 길이의 전체 Shorts로 확장한다.

```text
0~35초 / 36~50초 / 51~65초 / 66초 이상
```

작은 표본은 다음 방식으로 중앙값 0.5 쪽으로 축소한다.

```python
cohort_confidence = n / (n + 8)
sample_confidence = min(1, sqrt(sample / cohort_median_sample))
reliability = cohort_confidence * sample_confidence
adjusted = 0.5 + (percentile - 0.5) * reliability
```

조회수와 내부 품질을 분리해 `strategy_success`, `exposure_luck`, `hidden_success`, `weak_result`, `insufficient_data`로 분류한다. Shorts Feed의 `engagedViews / views`는 초반 몰입 대리지표일 뿐 실제 노출 클릭률로 표현하지 않는다. 업로드 시간은 저장하지만 표본이 충분해도 최대 보조 보정 범위를 넘지 않으며 현재 기본 점수에는 직접 넣지 않는다.

## Judge

```python
base_score = metric_score * 0.70 + qualitative_score * 0.20 + trend_novelty * 0.10
adjusted_score = base_score - duplicate - critic_risk - stale_strategy + exploration_bonus
```

정성 평가 영향은 20퍼센트로 제한한다. 페널티 상한은 중복 15점, Critic 위험 10점, 만료 전략 5점이다. 탐색 보너스는 adjacent 최대 3점, wildcard 최대 5점이다. 장기 70:20:10 탐색 목표는 `job_id` 해시로 재현한다.

표본 불확실성은 `adjusted_score`에서 따로 빼지 않는다. 이미 두 곳에서 반영되기 때문이다. `shrink_percentile`이 모든 지표를 같은 cohort 신뢰도만큼 중앙값 0.5로 당기고, `_score_blend_weights`가 신뢰도가 낮을 때 metric 가중치 자체를 낮춘다. 여기서 낮은 확신도를 세 번째로 차감하면 채널이 어릴 때 실행 가능 점수에 구조적으로 도달할 수 없다. 확신도는 아래 결정 단계에서 가장 강한 판정을 제한하는 용도로만 쓴다.

`confidence`는 cohort 신뢰도를 그대로 보고한다. 예전에는 후보 생성 시 0.70을 한 번 더 곱해 필드 이름과 값의 의미가 어긋났다.

### 동적 결정 임계값

`DECISIONS = (selected, limited_test, manual_review, rejected)` 네 값 중 실제로 다르게 동작하는 지점은 코드베이스 전체에서 딱 하나뿐이다: `judge_candidate()`의 `adjusted_score >= 임계값` 여부. `selected`와 `limited_test`는 카테고리 사용 기록(`record_category_usage`)과 critic-conflict 감지 어디에서도 구분되지 않고 항상 `{"selected", "limited_test"}`로 함께 취급되며, `manual_review`/`rejected`도 `4580d95`(2026-07-27) 이후 봇 메시지·제작 진행 여부가 동일해서 서로 구분되지 않는다. `selected` 전용 조건(`adjusted_score>=70 and confidence>=0.6`)은 지금까지 실측 adjusted_score가 70을 넘긴 적이 없어(2026-07-28 기준 최고 56.0) 사실상 죽어있는 코드다.

이 때문에 과거에는 고정값 `adjusted_score >= 55.0` 하나가 사실상 유일한 게이트였는데, 2026-07-28 기준 실측 `planning_runs.adjusted_score` 13건(36.3~56.0, 중앙값 ≈41.4)으로는 상위 1건(≈8%)만 통과하는 지나치게 엄격한 기준이었다. 이제 이 임계값은 고정 55 대신 `planning_runs.adjusted_score` 히스토리의 `CLAUDE_SELECTION_PERCENTILE`(기본 `0.5`=중앙값) 퍼센타일로 매 job마다 동적으로 계산한다(`_dynamic_decision_threshold`). 계산된 값과 표본 수는 `topic_plan.json`의 `planning.decision_threshold` / `decision_threshold_percentile` / `decision_threshold_sample_count`에 매번 기록되어 감사 가능하다. 히스토리가 아예 없을 때만 55.0으로 폴백한다.

2026-07-28 시점에는 표본이 13건뿐이라 중앙값을 골랐다. **TODO(표본이 늘어나면 재검토):**
- job/게시물 수가 통계적으로 유의미해지면(예: 50~100건 이상) `CLAUDE_SELECTION_PERCENTILE`을 60~75th로 올리는 것을 재검토한다.
- 지금은 `objective_type`별로 나누지 않고 전체 히스토리를 함께 쓴다. 표본이 늘면 목표별로 분리할지 재검토한다.
- `selected` 등급이 계속 `limited_test`와 동일하게 죽은 채로 둘지, 아니면 실제로 다른 처리(예: 완전 자동 업로드)를 부여할지 재검토한다.

## 연구·모델·비용

기본 설정:

```text
CLAUDE_PLANNER_MODEL=claude-haiku-4-5-20251001
CLAUDE_CRITIC_MODEL=claude-haiku-4-5-20251001
CLAUDE_AUDIT_MODEL=claude-haiku-4-5-20251001
CLAUDE_INTERPRETER_MODEL=claude-haiku-4-5-20251001
CLAUDE_PLANNER_MAX_TOKENS=2400
CLAUDE_CRITIC_MAX_TOKENS=1500
CLAUDE_AUDIT_MAX_TOKENS=1600
CLAUDE_INTERPRETER_MAX_TOKENS=900
CLAUDE_SELECTION_PERCENTILE=0.5
CLAUDE_JOB_BUDGET_USD=0.30
CLAUDE_DAILY_BUDGET_USD=1.00
CLAUDE_MAX_WEB_SEARCHES_PER_JOB=4
RESEARCH_MODE=adaptive
WEB_RESEARCH_MAX_USES=2
CASE_RESEARCH_MAX_USES=2
YOUTUBE_FEEDBACK_SYNC_TTL_HOURS=6
```

`adaptive` 연구는 최종 선정된 주제만 조사한다. PubMed 결과가 충분하면 일반 web research를 생략할 수 있고, `사례추적형`은 case research, `연구발견형`은 일반 web research를 실행한다.

모든 Claude 응답은 멀티턴의 각 turn까지 `claude_usage.jsonl`과 SQLite에 동시에 기록한다. 예상 비용은 Haiku/Sonnet 입력·출력, cache write/read, web search 요청을 합산한다. 이는 로컬 추정치이며 최종 청구는 Anthropic Console을 기준으로 확인한다.

## 업로드 및 리프레시

업로드 성공 시 작업 폴더에 다음 계약을 저장한다.

```json
{
  "job_id": "auto_20260720_071500",
  "video_id": "xxxxxxxxxxx",
  "uploaded_at": "2026-07-20T07:30:00+09:00"
}
```

동시에 `video_jobs`와 `content_features`를 연결한다. 목표 영상이 5편, 15편, 30편 경계에 도달하면 각각 Quick Review, Full Audit, baseline rebuild 감사 대상을 계산한다. 가중치 변경 제안은 기존 값의 ±10퍼센트로 clamp하며 자동 적용하지 않는다. 전략 가설은 업로드마다 TTL을 진행하고, D28 스냅샷이 생기면 지지 또는 반증을 한 번만 반영해 confidence를 Python으로 다시 계산한다.

## 검증

```bash
python3 -m unittest \
  tests.test_content_objectives \
  tests.test_claude_cost \
  tests.test_objective_planner \
  tests.test_goal_pipeline \
  tests.test_youtube_feedback \
  tests.test_script_runtime -v

python3 -m compileall -q dev/src
bash -n dev/run_goal.sh dev/sh/0_topic_plan.sh dev/sh/0_script.sh dev/sh/3_upload.sh
```
