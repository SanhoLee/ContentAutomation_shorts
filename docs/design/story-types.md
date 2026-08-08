# Story Types — 채널 4장르와 비중 운영

주제 자동 선정 시 결정론적으로 `story_type`을 붙이고 → `strategy.json`에 고정 → Stage 2가 타입별 골격에
맞춰 대본과 `role`을 쓴다. 화면에 무엇이 보일지(`visual`, `visual_query`)는 대본 승인 뒤 별도 스테이지
`scene_visuals`가 정한다(§7 참고). 기존 FORMAT/HOOK/Creative DNA/근거/피드백 루프는 그대로 둔다.

관련: `docs/design/objective-driven-content-planner.md` (주제 선정), Creative DNA(Phase 2),
`content_package.py`(Phase 3).

---

## 1. 4장르

| id | 한글 라벨 | 목표 | 기본 비중 |
|----|-----------|------|-----------|
| `principle_experience` | 원리·체험형 | 현상 역설 → 숨은 기전 → 짧은 확인 | 35% |
| `myth_bust` | 오해 교정형 | 통념 한 줄 → 근거의 범위 → 대안 | 30% |
| `habit_mechanism` | 습관·기전형 | 습관 하나 ↔ 뇌 기전 → 오늘 할 최소 행동 | 25% |
| `case_journey` | 사례·여정형 | 전형 인물의 짧은 여정 | 10% |

`case_journey`가 가장 낮은 이유는 사례가 근거를 대신할 수 없기 때문이다. 템플릿에도 다른 타입보다
엄격한 안전 규칙이 들어 있다(실명·지역 금지, 완치 표현 금지, 한계 명시 필수).

### role 시퀀스

| story_type | role 시퀀스 |
|------------|-------------|
| principle_experience | hook → familiar → hidden → mechanism → proof → reframe → cta |
| myth_bust | hook → why_believed → evidence → limit → alternative → contrast → cta |
| habit_mechanism | hook → situation → mechanism → evidence → minimal_action → if_skip → cta |
| case_journey | hook → stuck → turning → mechanism → small_result → general_lesson → cta |

기존 "감정 곡선"과 충돌하면 story_type 시퀀스가 이긴다. 감정 곡선은 톤 가이드로만 남는다.

---

## 2. 자동 / 수동 동작 차이

| 경로 | story_type을 정하는 주체 | 비중 적용 |
|------|--------------------------|-----------|
| `run_goal.sh`, `0_topic_plan.py plan`, `--from-eligible` | `objective_planner.resolve_story_type()` — 결정론 | O (`enforce_on_auto`) |
| 봇 수동 주제(`/run_review "문장"`), `0_script.py "주제"` | Stage 1 Claude가 4개 중 추론 | X (`enforce_on_manual: false`) |

수동 경로에서 Stage 1이 타입을 못 정하면 `default_story_type`(기본 `principle_experience`)으로 떨어진다.

### 선정 알고리즘 (LLM 없음)

최근 `lookback_jobs`개 잡의 실제 분포를 보고 **목표 비중 대비 가장 부족한 타입**을 고른다.

```
deficit[t] = mix[t] * (len(recent) + 1) - count[t]
pick = argmax(deficit), 동점이면 priority_on_tie 순서
```

최근 이력 출처는 순서대로: ① `data/work/*/strategy.json`의 `story_type`,
② 피드백 DB `content_features.format_type`을 매핑으로 환산(story_type 이전 영상 백필),
③ 둘 다 없으면 콜드 스타트(비중이 가장 높은 타입, `rng`를 주면 가중 랜덤).

`suggested_story_types`(eligible 후보의 힌트)는 **후보군을 좁힐 뿐 쿼터를 넘기지 못한다.**
힌트가 전부 초과분이면 무시하고 전역 선택으로 돌아간다. 즉 오해형 제목이 연달아 들어와도
믹스가 무너지지 않는다.

---

## 3. FORMAT_TYPES와의 관계

`story_type`은 기존 `format_type`을 대체하지 않고 상위 축으로 병렬 운영한다.

| format_type | story_type | | story_type | 대표 format_type |
|---|---|---|---|---|
| 오해반전형 | myth_bust | | principle_experience | 자가진단형 |
| 자가진단형 | principle_experience | | myth_bust | 오해반전형 |
| 비교형 | myth_bust | | habit_mechanism | 행동챌린지형 |
| 연구발견형 | principle_experience | | case_journey | 사례추적형 |
| 행동챌린지형 | habit_mechanism | | | |
| 사례추적형 | case_journey | | | |

**둘이 어긋나면 `story_type`이 이긴다.** `format_type`을 매핑값으로 덮어쓰고 경고를 남긴다
(`story_types.reconcile()`). 이미 일치하는 조합(예: myth_bust + 비교형)은 건드리지 않는다.

Stage 1이 기획 단계에서 확정된 타입을 무시하고 다른 값을 내면 `merge_planning_contract()`가
기획값으로 되돌린다. 그 슬롯의 믹스 쿼터는 후보 선정 시점에 이미 쓰였기 때문이다.

---

## 4. 산출물 스키마

`strategy.json`
```json
{ "story_type": "myth_bust", "format_type": "오해반전형", "hook_type": "반전형", ... }
```

Stage 2가 쓰는 씬 (`{text, role}`) — 여기까지가 대본이고, 승인 게이트 대상이다.
```json
{ "text": "...", "role": "why_believed" }
```

`scene_visuals` 이후의 `scenes.json` / `scenes_timed.json`
```json
{
  "text": "...", "role": "why_believed",
  "visual": { "type": "habit", "brief": "한 줄 연출 지시", "must_show": ["요소1"] },
  "visual_query": "english stock search keywords"
}
```

`visual.type` 허용값: `paradox | scale | mechanism | proof | habit | contrast | person | aftermath | broll`

`content_package.json`에는 `story_type`, `format_type`, `scenes[].visual`이 함께 실린다.
`video_meta.json`에도 `story_type` / `format_type`이 들어간다.

eligible 후보(`data/topics/eligible/*.json`)는 `suggested_story_types: []`와 `story_type: null`을 갖는다.
선정 시점에 planner가 확정한다.

---

## 5. B-roll 쿼리 우선순위

`broll_policy.scene_queries()`는 `visual.brief` → `must_show` → `visual_query` 순으로 후보를 만들되,
**로마자가 들어 있는 후보만** 실제 검색에 쓴다. Pexels는 영문 색인이라 한국어 쿼리는 결과가 0건이기
때문이다. 그래서 한국어 `brief`는 사람/후속 AI 영상 단계를 위한 연출 노트로 남고, 검색은 영문
`visual_query`가 담당한다. 영문 brief를 쓰면 그때는 brief가 1순위로 쓰인다.

---

## 6. 설정과 롤백

`dev/config/story_type_mix.json` — 비중, `lookback_jobs`, `enforce_on_auto/manual`,
`default_story_type`, `priority_on_tie`. mtime 캐시라 재시작 없이 반영된다.
비중 합이 1이 아니어도 자동 정규화되고, 타입을 0으로 두거나 빼면 그 타입은 선택되지 않는다.

`dev/config/story_templates/` — `_common.md` + 타입별 4개. 역시 mtime 캐시.

| 환경변수 | 기본값 | 의미 |
|----------|--------|------|
| `USE_STORY_TYPES` | `1` | `0`이면 Stage 1/Stage 2 프롬프트와 출력 스키마가 도입 이전과 **완전히 동일**해진다. `scene_visuals`도 `role`/`visual` 없이 `{text, visual_query}`만 남긴다 |
| `STORY_TYPE_MIX_PATH` | `dev/config/story_type_mix.json` | 비중 설정 경로 |
| `STORY_TEMPLATES_DIR` | `dev/config/story_templates` | 템플릿 폴더 경로 |

설정 파일이나 템플릿이 없어도 잡은 죽지 않는다. 비중은 코드 기본값으로, 템플릿이 없으면 story 블록
없이 기존 대본 경로로 떨어진다.

---

## 7. 시각 계획 스테이지 (`scene_visuals`)

`script.txt`는 `scenes.json`을 펼친 같은 객체라, 승인 게이트에서 본문을 고치면 두 파일이 어긋난다.
그래서 시각 계획은 Stage 2가 아니라 승인 **뒤**에 돈다 (`pipeline_flow.STAGES`: `script` → `scene_visuals`
→ `x_thread`). `advance()`가 스테이지 완료 **후에** 게이트에 멈추므로, 이 위치가 곧 "확정된 본문"이다.

순서: ① `script.txt` 문단 ↔ 씬 재동기화(개수가 다르면 문단 기준으로 재구성하고 role을 위치로 재부여)
→ ② Haiku 1회로 `visual` + `visual_query` 계획 → ③ 빠진 값은 결정론적으로 채움
→ ④ `content_package.json` 재생성(X/Instagram 어댑터가 수정된 본문을 보게).

`script_quality.json`에 기록되는 값:

- `metrics.story_type`(Stage 2), `metrics.scene_count` / `metrics.scenes_with_visual_brief` /
  `metrics.visual_plan_source`(`claude` | `fallback`)
- 경고: `visual_plan_unavailable`(계획 호출 실패 → 기본 검색어), `visual_brief_backfilled`(brief 누락 →
  대본 문장으로 채움), `role_off_sequence`, `role_bookends_missing`

계획 호출이 실패해도 **재호출하지 않는다**. 명세는 "재시도 1회 또는 guard 실패"를 허용하지만, 이 저장소
규칙(`CLAUDE.md`, `KNOWN_ISSUES.md`)은 API 비용을 두 배로 만드는 재시도보다 비용 없는 실패 처리를
우선한다. 같은 이유로 `stage_guard`에 script/scene_visuals 가드를 **붙이지 않았다** — 가드 실패는
`pipeline_flow.run_stage`에서 재실행을 부른다. 대신 위 검사는 비용 없이 돈다.

| 환경변수 | 기본값 | 의미 |
|----------|--------|------|
| `SCENE_VISUALS_PLAN` | `1` | `0`이면 Haiku 계획 호출을 끈다. 재동기화와 결정론적 채우기는 계속 동작 |
| `SCENE_VISUALS_MAX_TOKENS` | `2000` | 계획 응답 상한 |
| `SCENE_VISUALS_TIMEOUT_SEC` | `90` | 계획 호출 타임아웃 |

모델은 `CLAUDE_STRATEGY_MODEL`(기본 Haiku)을 그대로 쓴다. 실측 비용은 작업당 약 $0.0075.

범위 밖: 전 구간 AI 영상 생성, "핵심 3샷" 슬롯, FORMAT_TYPES 문자열 제거,
`content_objectives` 가중치 개편.
