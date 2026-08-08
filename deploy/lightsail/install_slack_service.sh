#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="${APP_ROOT:-$HOME/brain50}"
SERVICE="brain50-slack-dev.service"
UNIT_SRC="${APP_ROOT}/deploy/systemd/${SERVICE}"

if [[ ! -f "$UNIT_SRC" ]]; then
  echo "systemd unit not found: $UNIT_SRC" >&2
  exit 1
fi

sudo cp "$UNIT_SRC" "/etc/systemd/system/${SERVICE}"
sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"
sudo systemctl status "$SERVICE" --no-pager
