#!/usr/bin/env bash
set -euo pipefail

ENV_NAME="${1:-dev}"
APP_ROOT="${APP_ROOT:-$HOME/brain50}"
SERVICE="brain50-telegram-${ENV_NAME}.service"
UNIT_SRC="${APP_ROOT}/deploy/systemd/${SERVICE}"

if [[ "$ENV_NAME" != "dev" && "$ENV_NAME" != "prod" ]]; then
  echo "Usage: $0 [dev|prod]" >&2
  exit 2
fi
if [[ ! -f "$UNIT_SRC" ]]; then
  echo "systemd unit not found: $UNIT_SRC" >&2
  exit 1
fi

sudo cp "$UNIT_SRC" "/etc/systemd/system/${SERVICE}"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"
sudo systemctl status "$SERVICE" --no-pager
