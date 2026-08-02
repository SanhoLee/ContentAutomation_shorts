# 트렌드 조사 스케줄링 + 주제 선택

정기적으로 트렌드를 조사해 주제 후보 큐를 채우고, **상위 3개 중 하나를 사람이 고르는** 흐름입니다.

조사 자체는 Claude를 전혀 호출하지 않습니다. 시드 풀 → Google/YouTube 자동완성 → 결정론적 점수(`topic_score.py`)까지 전부 규칙 기반이라 몇 번을 돌려도 API 비용이 들지 않습니다.

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
| `data/topics/rejected/{topic_id}.json` | 탈락 후보 + `reject_reason` (`duplicate`/`ban_keyword`/`below_threshold`) |
| `data/topics/selected.json` | 사람이 마지막으로 고른 주제 (포인터 파일) |

`data/topics/`는 환경별 작업 상태라 gitignore 대상입니다.

### `status`와 `consumed`의 차이

- `status: "selected"` — **사람이 골랐다**는 뜻
- `consumed: true` — **파이프라인이 실제로 소비했다**는 뜻

선택은 `consumed`를 건드리지 않습니다. 둘을 분리해 둔 덕분에 기존 `pick_top_eligible()`(자동 경로에서 1등을 집어가는 함수)의 동작이 그대로 유지되고, 나중에 파이프라인을 연결할 때 "고르기만 한 주제"와 "이미 쓴 주제"를 구분할 수 있습니다.

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
```

`--select-rank N`은 `--top` 목록의 N번을 고릅니다. topic_id를 직접 지정하려면 `--select {topic_id}`를 쓰세요.

출력 예시:

```
1. 치매 예방 운동 방법 정리  (점수 90 · 시드 치매 · 채널 적합, 근거 가능성)
2. 뇌 건강 식단 인지 기능 연구  (점수 80 · 시드 치매 · 채널 적합, 근거 가능성)
3. 수면 부족과 기억력 저하 연구 결과  (점수 70 · 시드 치매 · 채널 적합, 근거 가능성)
```

괄호 안은 `topic_score.py`의 5개 채점 항목 중 점수가 가장 높았던 두 개입니다.

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
- 한 번 고른 후보는 다음 `--top` 목록에서 빠집니다. 다시 보려면 `--list-eligible`을 쓰세요.

## 아직 안 된 것

- 고른 주제로 제작을 시작하는 연결 (`selected.json` → `0_topic_plan.py` / `run_pipeline.py`)
- 텔레그램 봇 쪽 동일 흐름 (현재는 슬랙만)
