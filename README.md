# Shorts Automation Pipeline

AWS Lightsail에서 동작하는 AI 기반 YouTube Shorts 자동화 파이프라인입니다.

## 목적
- 최소한의 수동 작업으로 Shorts 콘텐츠를 **생산 → 편집 → 업로드**까지 자동화
- 현재는 품질 관리를 위해 **수동 확인 + 개입**을 중시
- 나중에는 병렬 실행, n8n 연동, AI 피드백 루프를 통해 더 높은 자동화 수준 목표

## 현재 상태 (개발 현황)
- **dev / prod 환경 분리** 완료
- 단계별 파이프라인:
  - 0. Script 생성 (AI 프롬프트)
  - 1. TTS (음성 생성)
  - 2. Caption / 자막
  - 3. Broll + 영상 렌더링
  - 4. YouTube 업로드
- Shell + Python 조합으로 구성
- 수동 실행 중심 (`run.sh` 또는 개별 sh 파일)

## 디렉토리 구조 (주요)
- `dev/`, `prod/` : 개발/운영 환경 분리
- `src/` : Python 핵심 스크립트
- `sh/` : Shell wrapper
- `data/assets/` : BGM, 템플릿 등 공유 자원
- `data/work/{JOB_ID}/` : 실행별 독립 작업 폴더

## 실행 방법

자세한 실행 가이드는 **[docs/usage/](docs/usage/)** 폴더를 참고하세요.

### 빠른 시작
```bash
cd ~/brain50/dev
./run.sh "오메가3가 정말 뇌에 좋을까?"
```

## 문서
- 기본 사용법 및 JOB_ID 사용법: [docs/usage/](docs/usage/)
- 향후 확장 계획: 병렬 실행, AI Agent 연동 등