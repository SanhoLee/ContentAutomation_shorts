#!/bin/bash

# secrets.sh 로드 (API 키 등 민감 정보)
SECRETS_FILE="$(dirname "${BASH_SOURCE[0]}")/secrets.sh"
if [ -f "$SECRETS_FILE" ]; then
    source "$SECRETS_FILE"
else
    echo "경고: secrets.sh 파일이 없습니다. ~/brain50/secrets.sh 를 생성하세요."
fi

# 환경 감지 (dev 또는 prod)
ENV=$(basename "$(dirname "$0")")

CONFIG_FILE="$(dirname "${BASH_SOURCE[0]}")/config.yaml"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "오류: $CONFIG_FILE 파일이 없습니다."
    exit 1
fi

# Python으로 YAML 파싱하여 환경변수로 export
python3 - <<EOF
import yaml
import os
import sys

with open("$CONFIG_FILE", "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

# common 설정 로드
for key, value in config.get("common", {}).items():
    os.environ[key] = str(value)

# 현재 환경(dev/prod) 설정 로드
env_config = config.get("$ENV", {})
for key, value in env_config.items():
    os.environ[key] = str(value)

print(f"[INFO] 설정 로드 완료: ENV=$ENV", file=sys.stderr)
EOF

# JOB_ID 처리 (없으면 자동 생성)
if [ -z "$JOB_ID" ]; then
    JOB_ID=$(date +%Y%m%d_%H%M%S)
fi
export JOB_ID

# WORK_DIR 동적 설정
export WORK_DIR="${WORK_DIR_BASE}/${JOB_ID}"

echo "[INFO] JOB_ID=$JOB_ID, WORK_DIR=$WORK_DIR"