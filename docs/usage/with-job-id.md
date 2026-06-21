# JOB_ID 함께 사용하기

특정 상황에서 JOB_ID를 직접 지정해서 실행하는 방법입니다.

## 사용법

```bash
cd ~/brain50/dev
./run.sh "주제 문장" [JOB_ID]
```

운영 환경도 동일합니다.

```bash
cd ~/brain50/prod
./run.sh "주제 문장" [JOB_ID]
```

### 예시
```bash
./run.sh "오메가3가 정말 뇌에 좋을까?" test_omega3_v1
./run.sh "운동이 기억력에 미치는 영향" weekly_20250620
```

## 언제 사용하나요?

- 같은 주제로 여러 버전을 테스트하고 비교하고 싶을 때
- 의미 있는 이름으로 결과 폴더를 관리하고 싶을 때 (예: test_v1, experiment_abc)
- AI Agent나 자동화 시스템이 고유 ID를 부여해서 실행할 때
- 디버깅이나 로그 추적을 쉽게 하고 싶을 때

## 저장 위치
- dev 작업 폴더: `~/brain50/dev/data/work/{JOB_ID}/`
- dev 최종 영상: `~/brain50/dev/data/output/output_{JOB_ID}.mp4`
- prod 작업 폴더: `~/brain50/prod/data/work/{JOB_ID}/`
- prod 최종 영상: `~/brain50/prod/data/output/output_{JOB_ID}.mp4`

## 주의사항
- JOB_ID는 영문, 숫자, 언더스코어(_) 조합을 추천합니다.
- 공백이나 특수문자는 피하세요.
- 같은 JOB_ID로 다시 렌더링하면 `output_{JOB_ID}.mp4`가 덮어써집니다.
