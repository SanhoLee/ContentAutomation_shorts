#!/bin/bash
set -e
source "$(dirname "$0")/../config.sh"

mkdir -p "$OUTPUT_DIR"

DURATION_OVERRIDE=""
FONT_SIZE="${CAPTION_FONT_SIZE:-22}"
MARGIN_V="${CAPTION_MARGIN_V:-60}"
MARGIN_H="${CAPTION_MARGIN_H:-10}"  # 좌우 마진 (PlayResX=384 기준, 10 ≈ 실제 28px)
CAPTION_STYLE_NAME="${CAPTION_STYLE:-default}"
CAPTION_STYLE_FILE="${CAPTION_STYLE_FILE:-$BASE_DIR/caption_styles.yaml}"

while [ "$#" -gt 0 ]; do
    case "$1" in
        --font-size)
            FONT_SIZE="$2"
            shift 2
            ;;
        --margin-v)
            MARGIN_V="$2"
            shift 2
            ;;
        --margin-h)
            MARGIN_H="$2"
            shift 2
            ;;
        --style|--caption-style)
            CAPTION_STYLE_NAME="$2"
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
    --font-size "$FONT_SIZE"
    --margin-v "$MARGIN_V"
)
if [ -n "$MARGIN_H" ]; then
    STYLE_ARGS+=(--margin-h "$MARGIN_H")
fi

CAPTION_FORCE_STYLE="$(python3 "$SRC_DIR/caption_style.py" "${STYLE_ARGS[@]}")"
CAPTION_STYLE_JSON="$(python3 "$SRC_DIR/caption_style.py" "${STYLE_ARGS[@]}" --json)"

echo "자막 설정: Style=${CAPTION_STYLE_NAME}, FontSize=${FONT_SIZE}, MarginV=${MARGIN_V}, MarginH=${MARGIN_H}"

RENDER_PROGRESS_FILE="$WORK_DIR/render_progress.txt"
rm -f "$RENDER_PROGRESS_FILE"

ffmpeg -y -nostats -progress "$RENDER_PROGRESS_FILE" \
-stream_loop -1 -i "$WORK_DIR/broll.mp4" \
-i "$WORK_DIR/voice.wav" \
-stream_loop -1 -i "$ASSETS_DIR/bgm.mp3" \
-t "$DURATION" \
-filter_complex "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,subtitles=${WORK_DIR}/subs.srt:force_style='${CAPTION_FORCE_STYLE}'[v]; \
[2:a]volume=0.15[bgm]; \
[1:a][bgm]amix=inputs=2:duration=first:dropout_transition=0:weights=1 1[aout]" \
-map "[v]" -map "[aout]" \
-c:v libx264 -c:a aac -pix_fmt yuv420p \
"$OUTPUT_FILE"

cat > "$WORK_DIR/render_config.json" <<EOF
{"font_size": "${FONT_SIZE}", "margin_v": "${MARGIN_V}", "margin_h": "${MARGIN_H}", "caption_style": "${CAPTION_STYLE_NAME}", "caption_style_file": "${CAPTION_STYLE_FILE}", "duration": "${DURATION}", "caption_force_style": ${CAPTION_STYLE_JSON}}
EOF

echo "$OUTPUT_FILE" > "$WORK_DIR/output_path.txt"
echo "완료! $OUTPUT_FILE 생성됨 (${DURATION}초)"
