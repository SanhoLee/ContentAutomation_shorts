#!/bin/bash
source "$(dirname "$0")/config.sh"
source "$(dirname "$0")/sh/notify.sh"

set -e
trap 'notify_error "파이프라인 실패 (단계: $CURRENT_STEP, line $LINENO)"' ERR

CURRENT_STEP="0_script"
"$BASE_DIR/sh/0_script.sh"

CURRENT_STEP="1_generate"
"$BASE_DIR/sh/1_generate.sh"

CURRENT_STEP="2_render"
"$BASE_DIR/sh/2_render.sh" "$@"

CURRENT_STEP="3_upload"
"$BASE_DIR/sh/3_upload.sh"

notify_success "영상 생성+업로드 완료 ($(date +%Y%m%d_%H%M%S))"