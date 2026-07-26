"""Render Instagram carousel card texts to static PNG images with Pillow.

Korean font resolution mirrors `youtube/2_render.sh`'s `drawtext_font_option()`
(`fc-match -f '%{file}' "Noto Sans CJK KR"`), so cards use the same typeface
already relied on for video captions instead of introducing a second font
dependency.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FALLBACK_FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
BACKGROUND_COLOR = (24, 24, 27)
TEXT_COLOR = (245, 245, 245)
MAX_FONT_SIZE = 72
MIN_FONT_SIZE = 28
MARGIN = 96


def resolve_korean_font() -> str:
    try:
        result = subprocess.run(
            ["fc-match", "-f", "%{file}", "Noto Sans CJK KR"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        path = result.stdout.strip()
        if path and Path(path).is_file():
            return path
    except (OSError, subprocess.SubprocessError):
        pass
    return FALLBACK_FONT_PATH


def _wrap_lines(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        box = draw.textbbox((0, 0), candidate, font=font)
        if box[2] - box[0] <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _fit_text(
    draw: ImageDraw.ImageDraw, text: str, font_path: str, canvas_size: tuple[int, int]
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    width, height = canvas_size
    max_width = width - 2 * MARGIN
    max_height = height - 2 * MARGIN
    font_size = MAX_FONT_SIZE
    while font_size >= MIN_FONT_SIZE:
        font = ImageFont.truetype(font_path, font_size)
        lines = _wrap_lines(draw, text, font, max_width)
        line_height = font.getbbox("가")[3] - font.getbbox("가")[1]
        block_height = int(line_height * 1.4 * len(lines))
        if block_height <= max_height:
            return font, lines
        font_size -= 4
    font = ImageFont.truetype(font_path, MIN_FONT_SIZE)
    return font, _wrap_lines(draw, text, font, max_width)


def render_card(
    text: str,
    output_path: Path,
    canvas_size: tuple[int, int] = (1080, 1350),
    font_path: str | None = None,
) -> Path:
    font_path = font_path or resolve_korean_font()
    image = Image.new("RGB", canvas_size, BACKGROUND_COLOR)
    draw = ImageDraw.Draw(image)

    font, lines = _fit_text(draw, text, font_path, canvas_size)
    line_height = font.getbbox("가")[3] - font.getbbox("가")[1]
    line_step = int(line_height * 1.4)
    block_height = line_step * len(lines)
    y = (canvas_size[1] - block_height) // 2

    for line in lines:
        box = draw.textbbox((0, 0), line, font=font)
        line_width = box[2] - box[0]
        x = (canvas_size[0] - line_width) // 2
        draw.text((x, y), line, font=font, fill=TEXT_COLOR)
        y += line_step

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, "PNG")
    return output_path


def render_card_set(texts: list[str], output_dir: Path) -> list[Path | None]:
    """Render one PNG per text, preserving position (None marks a skipped card).

    Positions are kept aligned with `texts` (rather than compacting the list)
    so callers can zip texts/paths together without a failed card silently
    shifting every later card's image onto the wrong text.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    font_path = resolve_korean_font()
    paths: list[Path | None] = []
    for index, text in enumerate(texts, start=1):
        card_path = output_dir / f"card_{index:02d}.png"
        try:
            render_card(text, card_path, font_path=font_path)
            paths.append(card_path)
        except (OSError, ValueError) as exc:
            print(f"⚠️  카드 {index} 렌더링 실패, 건너뜁니다: {exc}")
            paths.append(None)
    return paths
