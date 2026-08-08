#!/usr/bin/env bash
set -euo pipefail
sudo systemctl restart brain50-slack-dev.service
sudo systemctl status brain50-slack-dev.service --no-pager
