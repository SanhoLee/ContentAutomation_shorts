# 목표 지표 기반 Shorts 기획 엔진

이 문서는 `dev`에 구현된 목표 기반 자동 기획 흐름의 운영 계약을 설명한다. 기존 직접 주제 명령은 그대로 유지하며, 목표 기획은 별도 진입점으로 실행한다. `prod`에는 dev 제한 테스트와 사용자 승인 전까지 반영하지 않는다.

## 실행 흐름

```text
목표·선택적 씨드
→ 6시간 TTL 기준 YouTube read-only 동기화
→ D1_APPROX / D7 / D28 / ROLLING_90D 스냅샷
→ 길이·기간·topic family cohort 정규화
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

Planner와 Critic은 정성적 제안과 반증만 담당한다. 분위수, 신뢰도, 비용, 페널티, 최종 선택은 Python이 계산한다. AI가 입력에 없는 후보·영상·근거를 참조하거나 허용 enum 외 값을 반환하면 결과를 거부하고 deterministic fallback을 사용한다.

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

`--require-runnable`을 사용하면 `manual_review` 또는 `rejected` 상태에서 exit code 2로 중단한다. `run_goal.sh`은 이 옵션을 사용하므로 오래된 데이터나 거절된 후보를 자동 업로드하지 않는다.

## Telegram / Slack

```text
/run_goal subscriber_growth
/run_goal subscriber_growth 수면
/goal_status
/goal_report
```

봇의 `/run_goal`은 기획 후 기존 승인형 스크립트 검수 흐름으로 연결한다. `manual_review` 또는 `rejected`이면 `topic_plan.json`을 보존하고 제작을 시작하지 않는다.

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
adjusted_score = base_score - duplicate - critic_risk - low_confidence - stale_strategy + exploration_bonus
```

정성 평가 영향은 20퍼센트로 제한한다. 페널티 상한은 중복 15점, Critic 위험 10점, 낮은 확신도 15점, 만료 전략 5점이다. 탐색 보너스는 adjacent 최대 3점, wildcard 최대 5점이다. 장기 70:20:10 탐색 목표는 `job_id` 해시로 재현한다.

## 연구·모델·비용

기본 설정:

```text
CLAUDE_PLANNER_MODEL=claude-haiku-4-5-20251001
CLAUDE_CRITIC_MODEL=claude-haiku-4-5-20251001
CLAUDE_AUDIT_MODEL=claude-haiku-4-5-20251001
CLAUDE_PLANNER_MAX_TOKENS=1200
CLAUDE_CRITIC_MAX_TOKENS=900
CLAUDE_AUDIT_MAX_TOKENS=1600
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
