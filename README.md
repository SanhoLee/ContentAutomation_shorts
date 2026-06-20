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
- `src/` : Python 핵심 스크립트
- `scripts/` : Shell wrapper
- `config/dev/` , `config/prod/` : 환경별 설정
- `data/assets/` : BGM, 템플릿 등 공유 자원
- `data/work/` : 실행 중 임시 파일 (gitignore)

## 실행 방법

**개발 모드**
```bash
cd /path/to/project
./run.sh dev