#!/bin/bash
set -e
source "$(dirname "$0")/../config.sh"

mkdir -p "$OUTPUT_DIR"

DURATION_OVERRIDE=""
FONT_SIZE="${CAPTION_FONT_SIZE:-22}"
MARGIN_V="${CAPTION_MARGIN_V:-60}"
MARGIN_H="${CAPTION_MARGIN_H:-10}"  # 좌우 마진
CAPTION_STYLE_NAME="${CAPTION_STYLE:-default}"
CAPTION_STYLE_FILE="${CAPTION_STYLE_FILE:-$BASE_DIR/caption_styles.yaml}"
FONT_SIZE_EXPLICIT=0
MARGIN_V_EXPLICIT=0
MARGIN_H_EXPLICIT=0
OFFSET_X="${CAPTION_OFFSET_X:-}"
OFFSET_Y="${CAPTION_OFFSET_Y:-}"
POS_X=""
POS_Y=""
FRAME_MODE="${FRAME_MODE:-full}"
FRAME_TOP_STYLE_FILE="${FRAME_TOP_STYLE_FILE:-$BASE_DIR/frame_top_styles.yaml}"
FRAME_BOTTOM_STYLE_FILE="${FRAME_BOTTOM_STYLE_FILE:-$BASE_DIR/frame_bottom_styles.yaml}"
FRAME_TOP_PRESET="${FRAME_TOP_PRESET:-default}"
FRAME_BOTTOM_PRESET="${FRAME_BOTTOM_PRESET:-default}"
FRAME_TOP_PCT="${FRAME_TOP_PCT:-}"
FRAME_BOTTOM_PCT="${FRAME_BOTTOM_PCT:-}"
FRAME_BG_COLOR="${FRAME_BG_COLOR:-black}"
FRAME_TOP_TITLE="${FRAME_TOP_TITLE:-}"
FRAME_TOP_SUBTITLE="${FRAME_TOP_SUBTITLE:-}"
FRAME_TOP_MARGIN_PCT="${FRAME_TOP_MARGIN_PCT:-}"
FRAME_TOP_MARGIN_X_PCT="${FRAME_TOP_MARGIN_X_PCT:-}"
FRAME_BOTTOM_CHANNEL_NAME="${FRAME_BOTTOM_CHANNEL_NAME:-브레인피프티}"
BROLL_FIT_MODE="${BROLL_FIT_MODE:-cover}"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --font-size)
            FONT_SIZE="$2"
            FONT_SIZE_EXPLICIT=1
            shift 2
            ;;
        --margin-v)
            MARGIN_V="$2"
            MARGIN_V_EXPLICIT=1
            shift 2
            ;;
        --margin-h)
            MARGIN_H="$2"
            MARGIN_H_EXPLICIT=1
            shift 2
            ;;
        --style|--caption-style)
            CAPTION_STYLE_NAME="$2"
            shift 2
            ;;
        --offset-x)
            OFFSET_X="$2"
            shift 2
            ;;
        --offset-y)
            OFFSET_Y="$2"
            shift 2
            ;;
        --pos-x)
            POS_X="$2"
            shift 2
            ;;
        --pos-y)
            POS_Y="$2"
            shift 2
            ;;
        --frame-mode|--frame)
            FRAME_MODE="$2"
            shift 2
            ;;
        --frame-top-h|--top-h)
            FRAME_TOP_PCT="$(python3 - <<PY
print(round(float("$2") / 1920 * 100, 4))
PY
)"
            shift 2
            ;;
        --frame-bottom-h|--bottom-h)
            FRAME_BOTTOM_PCT="$(python3 - <<PY
print(round(float("$2") / 1920 * 100, 4))
PY
)"
            shift 2
            ;;
        --frame-top-pct|--top-pct)
            FRAME_TOP_PCT="$2"
            shift 2
            ;;
        --frame-top-margin-pct|--top-margin-pct|--top-margin)
            FRAME_TOP_MARGIN_PCT="$2"
            shift 2
            ;;
        --frame-top-margin-x-pct|--top-margin-x-pct|--top-margin-x|--top-side-margin-pct)
            FRAME_TOP_MARGIN_X_PCT="$2"
            shift 2
            ;;
        --frame-bottom-pct|--bottom-pct)
            FRAME_BOTTOM_PCT="$2"
            shift 2
            ;;
        --frame-top-preset|--top-preset)
            FRAME_TOP_PRESET="$2"
            shift 2
            ;;
        --frame-bottom-preset|--bottom-preset)
            FRAME_BOTTOM_PRESET="$2"
            shift 2
            ;;
        --frame-header-text|--header-text|--top-title)
            FRAME_TOP_TITLE="$2"
            shift 2
            ;;
        --top-subtitle)
            FRAME_TOP_SUBTITLE="$2"
            shift 2
            ;;
        --bottom-channel-name|--channel-name)
            FRAME_BOTTOM_CHANNEL_NAME="$2"
            shift 2
            ;;
        --broll-fit|--broll-fit-mode)
            BROLL_FIT_MODE="$2"
            shift 2
            ;;
        *)
            DURATION_OVERRIDE="$1"
            shift
            ;;
    esac
done

DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "$WORK_DIR/voice.wav")
echo "나레이션 길이: ${DURATION}초"

if [ -n "$DURATION_OVERRIDE" ]; then
    DURATION="$DURATION_OVERRIDE"
    echo "테스트 모드: ${DURATION}초만 렌더링"
fi

STYLE_ARGS=(
    --preset-file "$CAPTION_STYLE_FILE"
    --style "$CAPTION_STYLE_NAME"
)
# default 스타일은 기존 CAPTION_* 기본값을 유지하고, 다른 프리셋은 파일 값을 우선합니다.
# CLI로 넘긴 값은 항상 프리셋보다 우선합니다.
if [ "$CAPTION_STYLE_NAME" = "default" ] || [ "$FONT_SIZE_EXPLICIT" = "1" ]; then
    STYLE_ARGS+=(--font-size "$FONT_SIZE")
fi
if [ "$CAPTION_STYLE_NAME" = "default" ] || [ "$MARGIN_V_EXPLICIT" = "1" ]; then
    STYLE_ARGS+=(--margin-v "$MARGIN_V")
fi
if [ "$MARGIN_H_EXPLICIT" = "1" ] || { [ "$CAPTION_STYLE_NAME" = "default" ] && [ -n "$MARGIN_H" ]; }; then
    STYLE_ARGS+=(--margin-h "$MARGIN_H")
fi
if [ -n "$OFFSET_X" ]; then
    STYLE_ARGS+=(--offset-x "$OFFSET_X")
fi
if [ -n "$OFFSET_Y" ]; then
    STYLE_ARGS+=(--offset-y "$OFFSET_Y")
fi
if [ -n "$POS_X" ]; then
    STYLE_ARGS+=(--pos-x "$POS_X")
fi
if [ -n "$POS_Y" ]; then
    STYLE_ARGS+=(--pos-y "$POS_Y")
fi

CAPTION_ASS_FILE="$WORK_DIR/subs_styled.ass"
CAPTION_STYLE_JSON="$(python3 "$SRC_DIR/caption_style.py" "${STYLE_ARGS[@]}" --srt-in "$WORK_DIR/subs.srt" --ass-out "$CAPTION_ASS_FILE" --json)"

echo "자막 설정: Style=${CAPTION_STYLE_NAME}, FontSize=${FONT_SIZE}, MarginV=${MARGIN_V}, MarginH=${MARGIN_H}, OffsetX=${OFFSET_X:-preset}, OffsetY=${OFFSET_Y:-preset}, PosX=${POS_X:-preset}, PosY=${POS_Y:-preset}"
echo "프레임 설정: Mode=${FRAME_MODE}, TopPreset=${FRAME_TOP_PRESET}, BottomPreset=${FRAME_BOTTOM_PRESET}, TopMarginPct=${FRAME_TOP_MARGIN_PCT:-preset}, TopMarginXPct=${FRAME_TOP_MARGIN_X_PCT:-preset}, BrollFit=${BROLL_FIT_MODE}"

RENDER_PROGRESS_FILE="$WORK_DIR/render_progress.txt"
rm -f "$RENDER_PROGRESS_FILE"

drawtext_font_option() {
    local font_name="$1"
    local font_file="$2"
    local font_style="${3:-}"
    local font_pattern="$font_name"
    if [ -n "$font_style" ]; then
        font_pattern="${font_pattern}:style=${font_style}"
    fi
    if [ -z "$font_file" ] && command -v fc-match >/dev/null 2>&1; then
        font_file="$(fc-match -f '%{file}' "$font_pattern" || true)"
    fi
    if [ -n "$font_file" ]; then
        font_file="${font_file//\\/\\\\}"
        font_file="${font_file//:/\\:}"
        printf ":fontfile=%s" "$font_file"
    else
        printf ":font=%s" "$font_pattern"
    fi
}

if [ "$FRAME_MODE" = "framed" ]; then
    FRAME_HEADER_JSON="$WORK_DIR/frame_header.json"
    if [ -f "$FRAME_HEADER_JSON" ] && { [ -z "$FRAME_TOP_TITLE" ] || [ -z "$FRAME_TOP_SUBTITLE" ]; }; then
        FRAME_HEADER_EXPORTS="$(FRAME_HEADER_JSON="$FRAME_HEADER_JSON" python3 - <<'PY'
import json, os, shlex
with open(os.environ["FRAME_HEADER_JSON"], "r", encoding="utf-8") as f:
    data = json.load(f)
for env_key, json_key in (("FRAME_TOP_TITLE", "title"), ("FRAME_TOP_SUBTITLE", "subtitle")):
    value = str(data.get(json_key, "")).strip()
    if value:
        print(f"export {env_key}={shlex.quote(value)}")
PY
)"
        eval "$FRAME_HEADER_EXPORTS"
    fi
    FRAME_ARGS=(
        --top-file "$FRAME_TOP_STYLE_FILE"
        --top-preset "$FRAME_TOP_PRESET"
        --bottom-file "$FRAME_BOTTOM_STYLE_FILE"
        --bottom-preset "$FRAME_BOTTOM_PRESET"
        --channel-name "$FRAME_BOTTOM_CHANNEL_NAME"
    )
    if [ -n "$FRAME_TOP_TITLE" ]; then
        FRAME_ARGS+=(--top-title "$FRAME_TOP_TITLE")
    fi
    if [ -n "$FRAME_TOP_SUBTITLE" ]; then
        FRAME_ARGS+=(--top-subtitle "$FRAME_TOP_SUBTITLE")
    fi
    if [ -n "$FRAME_TOP_PCT" ]; then
        FRAME_ARGS+=(--top-height-pct "$FRAME_TOP_PCT")
    fi
    if [ -n "$FRAME_TOP_MARGIN_PCT" ]; then
        FRAME_ARGS+=(--top-margin-pct "$FRAME_TOP_MARGIN_PCT")
    fi
    if [ -n "$FRAME_TOP_MARGIN_X_PCT" ]; then
        FRAME_ARGS+=(--top-margin-x-pct "$FRAME_TOP_MARGIN_X_PCT")
    fi
    if [ -n "$FRAME_BOTTOM_PCT" ]; then
        FRAME_ARGS+=(--bottom-height-pct "$FRAME_BOTTOM_PCT")
    fi
    eval "$(python3 "$SRC_DIR/frame_style.py" "${FRAME_ARGS[@]}" --shell)"

    CONTENT_W=1080
    CONTENT_H="$FRAME_CONTENT_H"
    CONTENT_Y="$FRAME_TOP_H"
    if [ "$CONTENT_H" -le 0 ]; then
        echo "오류: FRAME_TOP_H + FRAME_BOTTOM_H가 1920보다 작아야 합니다."
        exit 1
    fi

    case "$BROLL_FIT_MODE" in
        cover)
            BROLL_FILTER="[0:v]scale=${CONTENT_W}:${CONTENT_H}:force_original_aspect_ratio=increase,crop=${CONTENT_W}:${CONTENT_H},fps=30[broll];"
            ;;
        contain)
            BROLL_FILTER="[0:v]scale=${CONTENT_W}:${CONTENT_H}:force_original_aspect_ratio=decrease,pad=${CONTENT_W}:${CONTENT_H}:(ow-iw)/2:(oh-ih)/2:${FRAME_BG_COLOR},fps=30[broll];"
            ;;
        blur-contain)
            BROLL_FILTER="[0:v]split=2[bga][fga];[bga]scale=${CONTENT_W}:${CONTENT_H}:force_original_aspect_ratio=increase,crop=${CONTENT_W}:${CONTENT_H},boxblur=20:1,fps=30[bg];[fga]scale=${CONTENT_W}:${CONTENT_H}:force_original_aspect_ratio=decrease,pad=${CONTENT_W}:${CONTENT_H}:(ow-iw)/2:(oh-ih)/2:color=black,fps=30[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2[broll];"
            ;;
        *)
            echo "오류: 지원하지 않는 BROLL_FIT_MODE=${BROLL_FIT_MODE} (cover|contain|blur-contain)"
            exit 1
            ;;
    esac

    FILTER_COMPLEX="color=c=${FRAME_BG_COLOR}:s=1080x1920:d=${DURATION}[base];${BROLL_FILTER}[base][broll]overlay=0:${CONTENT_Y}[framed]"
    TOP_FONT_OPTION="$(drawtext_font_option "$FRAME_TOP_FONT_NAME" "$FRAME_TOP_FONT_FILE" "$FRAME_TOP_FONT_STYLE")"
    BOTTOM_FONT_OPTION="$(drawtext_font_option "$FRAME_BOTTOM_FONT_NAME" "$FRAME_BOTTOM_FONT_FILE")"
    CURRENT_LABEL="[framed]"
    if [ -n "$FRAME_TOP_TITLE" ]; then
        TOP_TITLE_FILE="$WORK_DIR/frame_top_title.txt"
        printf "%s" "$FRAME_TOP_TITLE" > "$TOP_TITLE_FILE"
        FILTER_COMPLEX="${FILTER_COMPLEX};${CURRENT_LABEL}drawtext=textfile=${TOP_TITLE_FILE}${TOP_FONT_OPTION}:fontcolor=${FRAME_TOP_TITLE_COLOR}:fontsize=${FRAME_TOP_TITLE_SIZE}:x=${FRAME_TOP_TEXT_X}:y=${FRAME_TOP_TITLE_Y}[with_top_title]"
        CURRENT_LABEL="[with_top_title]"
    fi
    if [ -n "$FRAME_TOP_SUBTITLE" ]; then
        TOP_SUBTITLE_FILE="$WORK_DIR/frame_top_subtitle.txt"
        printf "%s" "$FRAME_TOP_SUBTITLE" > "$TOP_SUBTITLE_FILE"
        FILTER_COMPLEX="${FILTER_COMPLEX};${CURRENT_LABEL}drawtext=textfile=${TOP_SUBTITLE_FILE}${TOP_FONT_OPTION}:fontcolor=${FRAME_TOP_SUBTITLE_COLOR}:fontsize=${FRAME_TOP_SUBTITLE_SIZE}:x=${FRAME_TOP_TEXT_X}:y=${FRAME_TOP_SUBTITLE_Y}[with_top_subtitle]"
        CURRENT_LABEL="[with_top_subtitle]"
    fi
    if [ -n "$FRAME_BOTTOM_CHANNEL_NAME" ]; then
        BOTTOM_CHANNEL_FILE="$WORK_DIR/frame_bottom_channel.txt"
        printf "%s" "$FRAME_BOTTOM_CHANNEL_NAME" > "$BOTTOM_CHANNEL_FILE"
        FILTER_COMPLEX="${FILTER_COMPLEX};${CURRENT_LABEL}drawtext=textfile=${BOTTOM_CHANNEL_FILE}${BOTTOM_FONT_OPTION}:fontcolor=${FRAME_BOTTOM_FONT_COLOR}:fontsize=${FRAME_BOTTOM_FONT_SIZE}:x=(w-text_w)/2:y=${FRAME_BOTTOM_CHANNEL_Y}[with_bottom_channel]"
        CURRENT_LABEL="[with_bottom_channel]"
    fi
    if [ "$CURRENT_LABEL" != "[framed]" ]; then
        FILTER_COMPLEX="${FILTER_COMPLEX};${CURRENT_LABEL}subtitles=${CAPTION_ASS_FILE}[v]"
    else
        FILTER_COMPLEX="${FILTER_COMPLEX};[framed]subtitles=${CAPTION_ASS_FILE}[v]"
    fi
else
    FILTER_COMPLEX="[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,subtitles=${CAPTION_ASS_FILE}[v]"
fi

ffmpeg -y -nostats -progress "$RENDER_PROGRESS_FILE" \
-stream_loop -1 -i "$WORK_DIR/broll.mp4" \
-i "$WORK_DIR/voice.wav" \
-stream_loop -1 -i "$ASSETS_DIR/bgm.mp3" \
-t "$DURATION" \
-filter_complex "${FILTER_COMPLEX}; \
[2:a]volume=0.15[bgm]; \
[1:a][bgm]amix=inputs=2:duration=first:dropout_transition=0:weights=1 1[aout]" \
-map "[v]" -map "[aout]" \
-c:v libx264 -c:a aac -pix_fmt yuv420p \
"$OUTPUT_FILE"

cat > "$WORK_DIR/render_config.json" <<EOF
{"font_size": "${FONT_SIZE}", "margin_v": "${MARGIN_V}", "margin_h": "${MARGIN_H}", "caption_style": "${CAPTION_STYLE_NAME}", "caption_style_file": "${CAPTION_STYLE_FILE}", "caption_ass_file": "${CAPTION_ASS_FILE}", "frame_mode": "${FRAME_MODE}", "frame_top_preset": "${FRAME_TOP_PRESET}", "frame_top_margin_pct": "${FRAME_TOP_MARGIN_PCT}", "frame_top_margin_x_pct": "${FRAME_TOP_MARGIN_X_PCT}", "frame_bottom_preset": "${FRAME_BOTTOM_PRESET}", "frame_json": ${FRAME_JSON:-null}, "broll_fit_mode": "${BROLL_FIT_MODE}", "duration": "${DURATION}", "caption_force_style": ${CAPTION_STYLE_JSON}}
EOF

echo "$OUTPUT_FILE" > "$WORK_DIR/output_path.txt"
echo "완료! $OUTPUT_FILE 생성됨 (${DURATION}초)"
