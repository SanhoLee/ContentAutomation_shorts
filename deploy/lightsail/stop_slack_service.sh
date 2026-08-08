#!/usr/bin/env bash
set -euo pipefail
sudo systemctl stop brain50-slack-dev.service
sudo systemctl disable brain50-slack-dev.service
