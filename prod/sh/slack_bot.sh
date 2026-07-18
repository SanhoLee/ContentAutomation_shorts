#!/bin/bash
set -e
source "$(dirname "$0")/../config.sh"
python3 "$SRC_DIR/slack_bot.py"
