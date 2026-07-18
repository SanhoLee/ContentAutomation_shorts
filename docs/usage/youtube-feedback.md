# YouTube 피드백 기능 사용법

이 문서는 `dev/src/6_youtube_feedback.py`를 처음 쓰는 사람을 위한 쉬운 안내서입니다.

> 현재 확인 결과: 기능 코드는 저장소에 있지만 기본 위치의 `dev/data/youtube_feedback.db`는 아직 없습니다. OAuth 환경변수도 설정되어 있지 않습니다. 따라서 이 작업공간에는 실제 채널 데이터가 아직 반영되지 않았습니다. 아래 `sync`를 실제 채널 계정으로 성공시킨 뒤 확인해야 합니다.

## 1. 무엇을 하는 기능인가요?

명령은 세 개입니다.

```bash
python dev/src/6_youtube_feedback.py sync
python dev/src/6_youtube_feedback.py report
python dev/src/6_youtube_feedback.py check-topic "수면 부족과 기억력 저하"
```

- `sync`: YouTube에서 실제 데이터를 읽어 DB에 저장합니다.
- `report`: DB를 읽어 Markdown과 JSON 보고서를 만듭니다.
- `check-topic`: 새 주제가 기존 영상과 얼마나 비슷한지 알려줍니다.

`report`와 `check-topic`은 YouTube에 접속하지 않고 마지막 `sync` 때 저장한 DB만 읽습니다.

## 2. 두 YouTube API의 차이

| API | 쉽게 말하면 | 저장되는 정보 |
|---|---|---|
| YouTube Data API v3 | 내 채널에 어떤 영상이 있는지 확인 | 영상 ID, 제목, 설명, 게시일, 길이, 공개 조회수·좋아요·댓글 |
| YouTube Analytics API | 각 영상의 성과가 어땠는지 확인 | 분석 조회수, 참여 조회수, 평균 시청 시간·비율, 공유, 구독자 증감 |

둘 다 정상이어야 성과 보고서가 제대로 만들어집니다. Data API만 되면 `videos`에는 데이터가 있지만 `analytics`는 비어 있을 수 있습니다.

공식 문서:

- [YouTube Data API channels.list](https://developers.google.com/youtube/v3/docs/channels/list)
- [YouTube Analytics API reports.query](https://developers.google.com/youtube/analytics/reference/reports/query)
- [YouTube OAuth 2.0](https://developers.google.com/youtube/v3/guides/authentication)

## 3. 최초 1회 준비

Google Cloud Console의 같은 프로젝트에서 아래 두 API를 활성화합니다.

1. YouTube Data API v3
2. YouTube Analytics API

OAuth 데스크톱 앱 자격 증명의 `client_secret.json`을 준비합니다. 서비스 계정이 아니라 실제 채널을 관리하는 Google 사용자가 로그인하고 동의해야 합니다.

필요한 읽기 전용 권한은 두 개입니다.

```text
https://www.googleapis.com/auth/youtube.readonly
https://www.googleapis.com/auth/yt-analytics.readonly
```

기존 업로드 토큰을 덮어쓰지 말고 피드백 전용 토큰을 사용하세요.

### Linux/Lightsail

```bash
cd ~/brain50
export YOUTUBE_CLIENT_SECRET=/home/ubuntu/secrets/client_secret.json
export YOUTUBE_FEEDBACK_TOKEN=/home/ubuntu/secrets/youtube_feedback_token.json
export YOUTUBE_FEEDBACK_DB=/home/ubuntu/brain50/dev/data/youtube_feedback.db
```

### Windows PowerShell

```powershell
cd C:\path\to\short_pipeline
$env:YOUTUBE_CLIENT_SECRET = "C:\secrets\client_secret.json"
$env:YOUTUBE_FEEDBACK_TOKEN = "C:\secrets\youtube_feedback_token.json"
$env:YOUTUBE_FEEDBACK_DB = "$PWD\dev\data\youtube_feedback.db"
```

주의: 기존 `4_upload.py`는 `YOUTUBE_CLIENT_SECRET`을 비밀 문자열로 사용하지만, 이 피드백 명령은 `client_secret.json` 파일 경로로 사용합니다. 기존 운영 설정을 바꾸지 말고 피드백 명령을 실행하는 터미널에서만 설정하세요.

## 4. 실제 데이터 받기

```bash
python dev/src/6_youtube_feedback.py sync
```

처음 실행하면 브라우저가 열립니다. 실제 YouTube 채널을 관리하는 Google 계정으로 로그인하고 읽기 권한에 동의합니다.

정상 출력은 다음과 비슷합니다.

```text
채널: 내 채널 이름 (UC...)
동기화 완료: 영상 42개, Analytics 영상 18개
지원 Analytics 지표: views, engagedViews, averageViewDuration, ...
DB: /home/ubuntu/brain50/dev/data/youtube_feedback.db
```

확인할 것은 세 가지입니다.

1. `채널:` 뒤에 내 채널 이름과 `UC...` ID가 나오는가
2. `영상 N개`의 N이 1 이상인가
3. `Analytics 영상 N개`의 N이 1 이상인가

최근 90일에 성과가 없으면 Analytics 영상 수가 적을 수 있습니다. 계속 0이라면 아래 DB와 오류 확인 절차를 진행하세요.

## 5. DB에 실제 데이터가 들어갔는지 확인

### 가장 쉬운 한 번 확인

프로젝트 루트에서 실행합니다. 별도 SQLite 프로그램이 없어도 됩니다.

```bash
python -c "import sqlite3; c=sqlite3.connect('dev/data/youtube_feedback.db'); print('videos=',c.execute('SELECT COUNT(*) FROM videos').fetchone()[0]); print('analytics=',c.execute('SELECT COUNT(*) FROM analytics').fetchone()[0]); print('keywords=',c.execute('SELECT COUNT(*) FROM keywords').fetchone()[0]); print('last_success=',c.execute(\"SELECT finished_at,video_count FROM sync_runs WHERE status='success' ORDER BY run_id DESC LIMIT 1\").fetchone())"
```

정상 예시:

```text
videos= 42
analytics= 18
keywords= 230
last_success= ('2026-07-18T12:34:56+00:00', 42)
```

- `videos > 0`: Data API 결과가 실제 DB에 저장됨
- `analytics > 0`: Analytics API 결과가 실제 DB에 저장됨
- `last_success`가 `None`이 아님: 동기화 성공 기록이 있음
- `keywords > 0`: 제목·설명에서 키워드가 추출됨

`YOUTUBE_FEEDBACK_DB`를 다른 경로로 지정했다면 명령 안의 DB 경로도 같은 경로로 바꾸세요.

### 실제 영상 제목 확인

```bash
python -c "import sqlite3; c=sqlite3.connect('dev/data/youtube_feedback.db'); [print(r) for r in c.execute('SELECT video_id,title,view_count,published_at FROM videos ORDER BY published_at DESC LIMIT 5')]"
```

YouTube Studio에 보이는 최근 제목과 같다면 Data API가 올바른 채널을 읽은 것입니다.

### 실제 Analytics 값 확인

```bash
python -c "import sqlite3; c=sqlite3.connect('dev/data/youtube_feedback.db'); [print(r) for r in c.execute('SELECT video_id,views,engaged_views,average_view_percentage,performance_score,period_start,period_end FROM analytics ORDER BY fetched_at DESC LIMIT 5')]"
```

행이 나오고 성과 값이 하나 이상 있으면 Analytics API 결과가 저장된 것입니다. 채널이나 기간에서 지원되지 않는 지표는 `None`일 수 있습니다.

## 6. DB 파일을 직접 열어보기

가장 쉬운 방법은 무료 [DB Browser for SQLite](https://sqlitebrowser.org/)를 쓰는 것입니다.

1. 프로그램을 설치하고 실행합니다.
2. **Open Database**를 누릅니다.
3. `dev/data/youtube_feedback.db`를 선택합니다.
4. **Browse Data** 탭을 누릅니다.
5. 아래 테이블을 하나씩 선택합니다.

| 테이블 | 확인할 내용 |
|---|---|
| `videos` | 실제 영상 제목과 공개 통계 |
| `analytics` | 영상별 성과와 `performance_score` |
| `keywords` | 제목·설명에서 추출한 단어 |
| `sync_runs` | 최근 실행의 `status`가 `success`인지 |

서버에 `sqlite3` CLI가 있다면 다음처럼 직접 조회할 수 있습니다.

```bash
sqlite3 dev/data/youtube_feedback.db
```

```sql
.tables
.headers on
.mode column

SELECT run_id,finished_at,status,video_count,error_message
FROM sync_runs ORDER BY run_id DESC LIMIT 5;

SELECT video_id,title,view_count
FROM videos ORDER BY published_at DESC LIMIT 5;

SELECT video_id,views,engaged_views,average_view_percentage,performance_score
FROM analytics ORDER BY fetched_at DESC LIMIT 5;

.quit
```

수동으로 열 때는 값을 수정하지 말고 조회만 하는 것을 권장합니다.

## 7. 두 API가 각각 정상인지 판별하기

### Data API 정상 기준

- `sync`에 올바른 채널 이름과 ID가 표시됨
- `videos` 행 수가 1 이상임
- 최근 제목이 YouTube Studio와 일치함
- `videos.fetched_at`이 최근 실행 시각으로 갱신됨

### Analytics API 정상 기준

- `지원 Analytics 지표:` 뒤에 지표가 하나 이상 표시됨
- `analytics` 행 수가 1 이상임
- `period_start`, `period_end`가 채워져 있음
- `views`, `engaged_views`, `average_view_percentage` 중 하나 이상에 값이 있음
- 비교 가능한 영상이 있으면 `performance_score`가 계산됨

Analytics의 `views`와 Data API의 누적 `view_count`는 조회 기간이 다르므로 같지 않아도 정상입니다. Analytics 기본 기간은 실행일 이틀 전까지의 최근 90일입니다.

## 8. 보고서와 주제 검사

동기화 후 보고서를 만듭니다.

```bash
python dev/src/6_youtube_feedback.py report
```

생성 파일:

```text
dev/data/youtube_report.md
dev/data/youtube_strategy.json
```

새 주제를 검사합니다.

```bash
python dev/src/6_youtube_feedback.py check-topic "수면 부족과 기억력 저하"
```

- `허용`: 많이 겹치지 않음
- `검토`: 비슷한 부분이 있어 사람이 확인해야 함
- `중복 가능성 높음`: 기존 영상과 많이 겹칠 가능성이 큼

이 판정은 자동 차단이 아니라 참고용 경고입니다.

## 9. 자주 생기는 문제

### DB 파일이 없다

`sync`가 성공하지 않았거나 DB 경로가 다를 수 있습니다.

```bash
echo "$YOUTUBE_FEEDBACK_DB"
ls -l dev/data/youtube_feedback.db
```

### videos는 있는데 analytics가 0이다

- Google Cloud Console에서 YouTube Analytics API가 활성화됐는지 확인합니다.
- 토큰에 `yt-analytics.readonly` 권한이 있는지 확인합니다.
- 예전 토큰에 새 권한은 자동 추가되지 않을 수 있으므로 피드백 토큰을 별도로 다시 인증합니다.
- 채널을 관리하는 정확한 Google 계정으로 로그인했는지 확인합니다.

### 동기화가 실패했다

```bash
python -c "import sqlite3; c=sqlite3.connect('dev/data/youtube_feedback.db'); [print(r) for r in c.execute('SELECT run_id,finished_at,status,video_count,error_message FROM sync_runs ORDER BY run_id DESC LIMIT 5')]"
```

- `인증/권한 실패`: 계정, OAuth 동의, 두 읽기 권한 확인
- `할당량 초과`: Google Cloud Console의 API 할당량 확인
- `지원하지 않는 요청 또는 지표`: 채널/기간에서 지원되지 않는 지표 확인

토큰과 `client_secret.json` 내용은 출력하거나 Git에 커밋하지 마세요.

## 10. 최종 체크리스트

- [ ] Data API와 Analytics API를 모두 활성화했다.
- [ ] 별도 피드백 토큰으로 로그인했다.
- [ ] `sync`가 성공했다.
- [ ] `videos` 행 수가 1 이상이다.
- [ ] `analytics` 행 수가 1 이상이다.
- [ ] 최근 제목이 실제 채널과 일치한다.
- [ ] `sync_runs`의 최근 상태가 `success`다.
- [ ] `report` 파일 두 개가 생성된다.
- [ ] `check-topic`에서 유사 영상이 표시된다.
