#!/usr/bin/env bash
set -euo pipefail

ENV_NAME="${1:-dev}"
SERVICE="brain50-telegram-${ENV_NAME}.service"

if [[ "$ENV_NAME" != "dev" && "$ENV_NAME" != "prod" ]]; then
  echo "Usage: $0 [dev|prod]" >&2
  exit 2
fi

sudo systemctl stop "$SERVICE"
sudo systemctl disable "$SERVICE"
sudo systemctl status "$SERVICE" --no-pager || true
