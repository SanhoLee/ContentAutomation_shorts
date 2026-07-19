#!/usr/bin/env bash
set -euo pipefail
ENV_NAME="${1:-dev}"; SERVICE="brain50-slack-${ENV_NAME}.service"
case "$ENV_NAME" in dev|prod) ;; *) echo "Usage: $0 [dev|prod]" >&2; exit 2;; esac
sudo systemctl restart "$SERVICE"
