#!/usr/bin/env bash
set -euo pipefail
sudo journalctl -u brain50-slack-dev.service -f
