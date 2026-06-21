# 기본 사용법 (주제만 입력)

가장 일반적인 실행 방법입니다.

## 개발 환경 실행

```bash
cd ~/brain50/dev
./run.sh "주제 문장"
```

### 예시
```bash
./run.sh "오메가3가 정말 뇌에 좋을까?"
./run.sh "운동이 기억력에 미치는 영향"
```

## 운영 환경 실행

```bash
cd ~/brain50/prod
./run.sh "주제 문장"
```

운영 환경도 개발 환경과 같은 인자를 사용합니다. 업로드는 기본적으로 YouTube 비공개 상태로 생성됩니다.

## 동작
- JOB_ID는 자동으로 현재 시간 (`20250620_141500`)으로 생성됩니다.
- 개발 환경 결과는 `~/brain50/dev/data/work/{JOB_ID}/`와 `~/brain50/dev/data/output/output_{JOB_ID}.mp4`에 저장됩니다.
- 운영 환경 결과는 `~/brain50/prod/data/work/{JOB_ID}/`와 `~/brain50/prod/data/output/output_{JOB_ID}.mp4`에 저장됩니다.
- `data/work/{JOB_ID}/output_path.txt`에는 해당 실행에서 생성한 최종 mp4 경로가 기록됩니다.

## 장점
- 주제만 바꿔 실행할 수 있습니다.
- dev/prod 실행 방식이 같아서 운영 반영 전 검증이 쉽습니다.
- JOB_ID별 작업 폴더와 output 파일이 분리되어 이전 산출물을 잘못 업로드할 위험을 줄입니다.
