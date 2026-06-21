#!/bin/bash
set -e
source "$(dirname "$0")/../config.sh"

mkdir -p "$OUTPUT_DIR"

DURATION_OVERRIDE=""
FONT_SIZE="${CAPTION_FONT_SIZE:-20}"
MARGIN_V="${CAPTION_MARGIN_V:-200}"

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

echo "자막 설정: FontSize=${FONT_SIZE}, MarginV=${MARGIN_V}"

ffmpeg -y \
-stream_loop -1 -i "$WORK_DIR/broll.mp4" \
-i "$WORK_DIR/voice.wav" \
-stream_loop -1 -i "$ASSETS_DIR/bgm.mp3" \
-t "$DURATION" \
-filter_complex "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,subtitles=${WORK_DIR}/subs.srt:force_style='FontName=Noto Sans CJK KR,FontSize=${FONT_SIZE},PrimaryColour=&H00FFFFFF,Outline=2,BorderStyle=3,MarginV=${MARGIN_V}'[v]; \
[2:a]volume=0.15[bgm]; \
[1:a][bgm]amix=inputs=2:duration=first:dropout_transition=0:weights=1 1[aout]" \
-map "[v]" -map "[aout]" \
-c:v libx264 -c:a aac -pix_fmt yuv420p \
"$OUTPUT_FILE"

cat > "$WORK_DIR/render_config.json" <<EOF
{"font_size": "${FONT_SIZE}", "margin_v": "${MARGIN_V}", "duration": "${DURATION}"}
EOF

echo "$OUTPUT_FILE" > "$WORK_DIR/output_path.txt"
echo "완료! $OUTPUT_FILE 생성됨 (${DURATION}초)"
