#!/bin/bash

# secrets.sh 로드 (API 키 등 민감 정보)
SECRETS_FILE="$(dirname "${BASH_SOURCE[0]}")/secrets.sh"
if [ -f "$SECRETS_FILE" ]; then
    source "$SECRETS_FILE"
else
    echo "경고: secrets.sh 파일이 없습니다. ~/brain50/secrets.sh 를 생성하세요."
fi

export BASE_DIR="$HOME/brain50/prod"
export SRC_DIR="$BASE_DIR/src"
export WORK_DIR="$BASE_DIR/data/work"
export ASSETS_DIR="$BASE_DIR/data/assets"
export OUTPUT_DIR="$BASE_DIR/data/output"
export BACKUP_DIR="$BASE_DIR/data/backups"
export ATEMPO="1.15"            # 1.0=기본속도, 1.1~1.3=더 빠르게 (0.05 단위 추천)
export TARGET_DURATION_SEC="60"
export CHARS_PER_SEC="4.66"      # 한국어 TTS 기본 속도 (실측 후 조정 가능)