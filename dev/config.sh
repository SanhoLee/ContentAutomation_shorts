#!/bin/bash

CONFIG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_NAME="$(basename "$CONFIG_DIR")"

# systemd 서비스는 로그인 shell보다 PATH가 짧을 수 있으므로 사용자 설치 bin을 보강합니다.
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

# secrets.sh 로드 (API 키 등 민감 정보)
SECRETS_FILE="$CONFIG_DIR/secrets.sh"
if [ -f "$SECRETS_FILE" ]; then
    source "$SECRETS_FILE"
else
    echo "경고: $SECRETS_FILE 파일이 없습니다. API 키가 필요한 단계는 실패할 수 있습니다."
fi

CONFIG_FILE="$CONFIG_DIR/config.yaml"

if [ ! -f "$CONFIG_FILE" ]; then
    echo "오류: $CONFIG_FILE 파일이 없습니다."
    exit 1
fi

# YAML 값을 shell export 문으로 변환해 현재 shell에 반영합니다.
CONFIG_EXPORTS="$(
CONFIG_FILE="$CONFIG_FILE" ENV_NAME="$ENV_NAME" python3 - <<'PY'
import yaml
import os
import sys
import re
import shlex

config_file = os.environ["CONFIG_FILE"]
env_name = os.environ["ENV_NAME"]

with open(config_file, "r", encoding="utf-8") as f:
    config = yaml.safe_load(f)

merged = {}
merged.update(config.get("common", {}) or {})
merged.update(config.get(env_name, {}) or {})

if not merged:
    print(f"오류: config.yaml에 '{env_name}' 환경 설정이 없습니다.", file=sys.stderr)
    sys.exit(1)

pattern = re.compile(r"\$\{([^}]+)\}")

def expand(value):
    value = str(value)
    for _ in range(10):
        next_value = pattern.sub(lambda m: str(merged.get(m.group(1), os.environ.get(m.group(1), m.group(0)))), value)
        if next_value == value:
            return next_value
        value = next_value
    return value

for key in list(merged.keys()):
    merged[key] = expand(merged[key])

for key, value in merged.items():
    print(f"export {key}={shlex.quote(str(value))}")
print(f"export ENV_NAME={shlex.quote(env_name)}")
print(f"[INFO] 설정 로드 완료: ENV={env_name}", file=sys.stderr)
PY
)" || exit 1

eval "$CONFIG_EXPORTS"

# JOB_ID 처리 (없으면 자동 생성)
if [ -z "$JOB_ID" ]; then
    JOB_ID=$(date +%Y%m%d_%H%M%S)
fi
export JOB_ID

# JOB_ID별 작업/산출물 위치
export WORK_DIR="${WORK_DIR_BASE}/${JOB_ID}"
export OUTPUT_FILE="${OUTPUT_DIR}/output_${JOB_ID}.mp4"

mkdir -p "$WORK_DIR" "$OUTPUT_DIR" "$BACKUP_DIR"

echo "[INFO] JOB_ID=$JOB_ID, WORK_DIR=$WORK_DIR, OUTPUT_FILE=$OUTPUT_FILE"