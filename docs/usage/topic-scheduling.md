# 트렌드 조사 스케줄링 + 주제 선택

정기적으로 트렌드를 조사해 주제 후보 큐를 채우고, **상위 3개 중 하나를 사람이 고르는** 흐름입니다.

조사 자체는 Claude를 전혀 호출하지 않습니다. 시드 풀 → Google/YouTube 자동완성 → 결정론적 점수(`topic_score.py`)까지 전부 규칙 기반이라 몇 번을 돌려도 API 비용이 들지 않습니다.

후보는 **베이스 분야 안에서만** 뽑힙니다. 자세한 설정은 아래 [베이스 분야 설정](#베이스-분야-설정)을 보세요.

## 전체 흐름

```
(스케줄러 또는 수동)
  refresh_topics.sh
    └─ topic_candidate_pipeline.py --refresh     시드 → 자동완성 → 점수 → 큐 기록
    └─ topic_candidate_pipeline.py --top 3       상위 3개 추천 출력
    └─ (--notify-slack) 슬랙에 선택 버튼 카드 게시

(사람)
  CLI --select-rank 1  또는  슬랙 버튼 클릭
    └─ data/topics/selected.json 에 선택 기록
```

**선택은 기록까지만 합니다.** 고른 주제로 실제 제작을 시작하는 것은 아직 수동이며, `data/topics/selected.json`이 나중에 파이프라인을 이어붙일 지점입니다.

## 산출물 위치

| 경로 | 내용 |
|------|------|
| `data/topics/raw/{날짜}_{시각}_run.json` | 조사 1회분 실행 기록 (시드별 성공/실패, 후보 수) |
| `data/topics/eligible/{topic_id}.json` | 점수 통과 후보. 선택되면 `status`가 `selected`로 바뀝니다 |
| `data/topics/rejected/{topic_id}.json` | 탈락 후보 + `reject_reason` (`duplicate`/`ban_keyword`/`no_domain_anchor`/`below_threshold`) |
| `data/topics/selected.json` | 사람이 마지막으로 고른 주제 (포인터 파일) |

`data/topics/`는 환경별 작업 상태라 gitignore 대상입니다.

### `status`와 `consumed`의 차이

- `status: "selected"` — **사람이 골랐다**는 뜻
- `consumed: true` — **파이프라인이 실제로 소비했다**는 뜻

선택은 `consumed`를 건드리지 않습니다. 둘을 분리해 둔 덕분에 기존 `pick_top_eligible()`(자동 경로에서 1등을 집어가는 함수)의 동작이 그대로 유지되고, 나중에 파이프라인을 연결할 때 "고르기만 한 주제"와 "이미 쓴 주제"를 구분할 수 있습니다.

## 베이스 분야 설정

`dev/config/topic_domain.json`이 "이 채널이 다루는 분야"를 정의합니다. 후보 선정은 이 범위를 벗어나지 못합니다.

| 키 | 뜻 |
|------|------|
| `anchor_terms` | 이 분야에 속한다고 볼 단어 목록. 후보 제목에 이 중 하나도 없으면 탈락합니다 |
| `require_anchor` | `true`면 앵커 없는 후보를 하드 컷. `false`면 점수만 깎고 통과 가능 |
| `seed_anchors` | 위험인자 시드에 붙일 접두어. `고혈압` → `치매 고혈압`으로 자동완성을 칩니다 |
| `seed_anchors_latin` | 영문 시드용 접두어. `diabetes` → `dementia diabetes` (`치매 diabetes`는 아무도 검색하지 않습니다) |
| `max_anchor_variants` | 시드 하나가 만들 앵커 변형 개수. 늘리면 자동완성 호출량도 늘어납니다 |
| `category_roles` | `research_categories.json`의 카테고리별 역할. `risk_factor`는 앵커를 붙이고 `core`는 그대로 씁니다 |
| `default_role` | `category_id`가 없는 시드(발행 키워드 이력, `extra_seeds`)의 역할 |

### 왜 필요한가

`research_categories.json`의 `chronic_disease` 카테고리는 PubMed 쿼리가 `"(diabetes OR hypertension) AND cognitive decline"`인데, 시드로 뽑히는 건 `keywords` 배열의 낱말(`고혈압`, `심혈관`…)뿐이라 **`AND cognitive decline` 절반이 빠진 채로** 자동완성을 쳤습니다. 그 결과 `심혈관질환 증상` 같은 순수 심장내과 검색어가 후보에 올라왔습니다. `trend_probe`의 drift 게이트도 이걸 못 잡습니다 — 채널 어휘에 `심혈관`이 들어 있어 domain_match가 1.0으로 나오기 때문입니다.

`topic_domain.json`은 그 빠진 절반을 두 군데에 다시 넣습니다. 시드 단계(앵커 부착)와 후보 단계(앵커 없으면 탈락)입니다.

### 다른 분야로 바꾸려면

`anchor_terms` / `seed_anchors` / `category_roles` 세 개만 고치면 됩니다. 코드 수정은 필요 없습니다. 파일이 없거나 깨져도 `topic_domain.py`의 내장 기본값(뇌 건강)으로 동작하므로, 실수로 지워도 게이트가 풀리지는 않습니다.

### 특정 후보가 왜 떨어졌는지 확인

```bash
python3 src/common/topic_candidate_pipeline.py --score-text "심혈관질환 증상"
```

```
베이스 분야: 뇌 건강(brain_health) · 앵커 19개 · 앵커 요구 필수
후보: 심혈관질환 증상
점수 40 → 탈락 (no_domain_anchor)
  뇌 관련성       0.0  (가중치 30)
  채널 적합       5.0  (가중치 15)
  검색 의도      15.0  (가중치 15)
  근거 가능성      0.0  (가중치 20)
  신선도        15.0  (가중치 15)
  안전 표현       5.0  (가중치 5)
앵커 매칭: 없음
어휘 매칭: 심혈관
```

네트워크를 타지 않고 큐도 건드리지 않으므로, 가중치나 `threshold`를 조정한 뒤 통과/탈락 경계를 확인할 때 씁니다.

## 수동 실행

```bash
cd dev

# 조사 + 상위 3개 출력
./sh/common/refresh_topics.sh

# 슬랙에도 선택 버튼 카드를 보내려면
./sh/common/refresh_topics.sh --notify-slack
```

개별 명령을 직접 쓸 수도 있습니다.

```bash
python3 src/common/topic_candidate_pipeline.py --refresh          # 큐 갱신
python3 src/common/topic_candidate_pipeline.py --top 3            # 상위 3개 추천
python3 src/common/topic_candidate_pipeline.py --select-rank 1    # 1번 선택
python3 src/common/topic_candidate_pipeline.py --selected         # 현재 선택 확인
python3 src/common/topic_candidate_pipeline.py --list-eligible    # 큐 전체 보기
python3 src/common/topic_candidate_pipeline.py --dry-run          # 기록 없이 후보만 확인
python3 src/common/topic_candidate_pipeline.py --score-text "..."  # 임의 문장 점수·앵커 확인
```

`--select-rank N`은 `--top` 목록의 N번을 고릅니다. topic_id를 직접 지정하려면 `--select {topic_id}`를 쓰세요.

출력 예시:

```
1. 치매 예방 운동 방법 정리  (점수 90 · 시드 치매 · 뇌 관련성, 근거 가능성)
2. 뇌 건강 식단 인지 기능 연구  (점수 80 · 시드 치매 · 뇌 관련성, 근거 가능성)
3. 수면 부족과 기억력 저하 연구 결과  (점수 70 · 시드 치매 · 뇌 관련성, 채널 적합)
```

괄호 안은 `topic_score.py`의 6개 채점 항목 중 점수가 가장 높았던 두 개입니다.

## 슬랙에서 선택

봇이 떠 있으면 `/topics`로 상위 3개 카드를 부를 수 있습니다. 홈 화면의 `주제 후보` 버튼도 같은 카드를 엽니다.

- **후보 버튼** — 선택만 기록합니다. 제작은 시작되지 않습니다.
- **다시 조사** — 트렌드 조사를 다시 돌린 뒤 새 목록을 보여줍니다 (1~2분 소요).

주제 선택은 다른 작업이 진행 중이어도 누를 수 있습니다. 큐만 건드리고 job 상태는 전혀 만지지 않기 때문입니다. 반면 `다시 조사`는 네트워크를 타므로 작업 중에는 막힙니다.

`refresh_topics.sh --notify-slack`이 보내는 카드도 봇이 보내는 것과 완전히 같습니다. 스케줄러가 별도 프로세스에서 카드를 올려도, 버튼을 누르면 돌고 있는 봇이 받아 처리합니다.

## 스케줄 등록

아직 등록하지 않았습니다. 아래 둘 중 하나를 골라 서버에서 직접 등록하세요.

### 방법 1: systemd timer (권장)

기존 봇 서비스(`deploy/systemd/brain50-*.service`)와 같은 방식이라 로그가 `journalctl`로 통합됩니다.

`/etc/systemd/system/brain50-topics-dev.service`:

```ini
[Unit]
Description=Brain50 Topic Candidate Refresh (dev)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=ubuntu
WorkingDirectory=/home/ubuntu/brain50/dev
ExecStart=/home/ubuntu/brain50/dev/sh/common/refresh_topics.sh --notify-slack
Environment=PYTHONUNBUFFERED=1
```

`/etc/systemd/system/brain50-topics-dev.timer`:

```ini
[Unit]
Description=Brain50 Topic Candidate Refresh schedule (dev)

[Timer]
OnCalendar=*-*-* 06:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

`OnCalendar`는 서버의 시스템 시간대를 따릅니다. Lightsail 기본값은 UTC이므로 한국시간 기준으로 돌리려면 `Timer` 섹션에 `Timezone=Asia/Seoul`을 추가하거나 UTC로 환산해 적으세요.

등록:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now brain50-topics-dev.timer

systemctl list-timers brain50-topics-dev.timer   # 다음 실행 시각 확인
journalctl -u brain50-topics-dev.service -n 50   # 마지막 실행 로그
sudo systemctl start brain50-topics-dev.service  # 스케줄과 무관하게 즉시 1회 실행
```

### 방법 2: crontab 한 줄

```bash
crontab -e
```

```cron
30 6 * * * cd /home/ubuntu/brain50/dev && ./sh/common/refresh_topics.sh --notify-slack >> data/ops/refresh_topics.log 2>&1
```

cron은 로그인 shell이 아니라 PATH가 짧습니다. `config.sh`가 `$HOME/.local/bin`을 PATH에 넣어주므로 대개 문제없지만, 실패하면 위 로그 파일을 먼저 확인하세요.

## 주의

- 조사는 **발행이 아니므로** `schedule_policy.py`의 일일 한도(`dev/config/schedules.yaml`)를 소모하지 않습니다. 하루에 여러 번 돌려도 업로드 쿼터에 영향이 없습니다.
- 자동완성 API가 채널 주제와 맞는 결과를 못 주면 해당 시드는 조용히 버려집니다(`trend_probe.py`의 드리프트 게이트). 후보가 0개로 나오면 `data/topics/raw/`의 최근 run 파일에서 `seed_reports`를 확인하세요.
- 앵커 시드(`치매 고혈압`)는 복합어라 자동완성 결과가 얇을 수 있습니다. 드리프트 게이트에 걸리면 원본 시드(`고혈압`)로 1회 폴백하고, 그때 들어온 비(非)뇌 후보는 `no_domain_anchor`로 걸러집니다. run 파일의 `anchor_fallback_count`가 `anchored_seed_count`의 절반을 넘으면 `seed_anchors`를 더 흔한 표현으로 바꾸는 걸 검토하세요.
- 한 번 고른 후보는 다음 `--top` 목록에서 빠집니다. 다시 보려면 `--list-eligible`을 쓰세요.

## 아직 안 된 것

- 고른 주제로 제작을 시작하는 연결 (`selected.json` → `0_topic_plan.py` / `run_pipeline.py`)
- 텔레그램 봇 쪽 동일 흐름 (현재는 슬랙만)
