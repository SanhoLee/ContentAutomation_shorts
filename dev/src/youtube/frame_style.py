#!/usr/bin/env python3
"""Resolve framed Shorts top/bottom safe-zone presets."""
import argparse
import json
import os
from pathlib import Path
import shlex

CANVAS_W = 1080
CANVAS_H = 1920

ALIASES = {
    "height_percent": "height_pct",
    "bg_color": "background",
    "font_name": "font",
    "font_color": "color",
    "title_font_color": "title_color",
    "subtitle_font_color": "subtitle_color",
    "font_size": "size",
    "title_font_size": "title_size",
    "subtitle_font_size": "subtitle_size",
    "margin_y": "bottom_margin_px",
    "margin_top_pct": "top_margin_pct",
    "margin_x_pct": "side_margin_pct",
}


def parse_scalar(value: str):
    value = value.strip()
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def load_presets(path: Path) -> dict:
    presets = {}
    current = None
    in_presets = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0 and stripped == "presets:":
            in_presets = True
            continue
        if not in_presets:
            continue
        if indent == 2 and stripped.endswith(":"):
            current = stripped[:-1].strip()
            presets[current] = {}
        elif indent >= 4 and current and ":" in stripped:
            key, value = stripped.split(":", 1)
            presets[current][ALIASES.get(key.strip(), key.strip())] = parse_scalar(value)
    if not presets:
        raise SystemExit(f"no frame presets found: {path}")
    return presets


def resolve(presets: dict, name: str, seen=None) -> dict:
    seen = seen or []
    if name not in presets:
        raise SystemExit(f"unknown frame preset '{name}'. available: {', '.join(sorted(presets))}")
    if name in seen:
        raise SystemExit(f"frame preset inheritance cycle: {' -> '.join(seen + [name])}")
    raw = dict(presets[name])
    parent = raw.pop("extends", None)
    result = resolve(presets, parent, seen + [name]) if parent else {}
    if "color" in raw:
        for key in ("title_color", "subtitle_color"):
            if key not in raw:
                result.pop(key, None)
    result.update(raw)
    return result


def int_from_pct(value, total):
    if value in (None, ""):
        return 0
    return int(round(float(str(value).rstrip("%")) * total / 100.0))


def int_value(data, key, default):
    value = data.get(key, default)
    if value in (None, ""):
        value = default
    return int(round(float(value)))


def normalize_keys(data):
    return {ALIASES.get(key, key): value for key, value in data.items()}


def text_width_units(text):
    units = 0.0
    for char in str(text or ""):
        if char.isspace():
            units += 0.35
        elif ord(char) < 128:
            units += 0.55
        else:
            units += 1.0
    return max(units, 1.0)


def max_line_width_units(text):
    return max(text_width_units(line) for line in str(text or "").splitlines() or [""])


def wrap_subtitle_if_needed(subtitle, font_size, max_width):
    """Wrap an over-wide subtitle at the most balanced word boundary."""
    subtitle = str(subtitle or "")
    existing_lines = subtitle.splitlines()
    if len(existing_lines) > 1:
        return subtitle, len(existing_lines)
    if text_width_units(subtitle) * font_size <= max_width:
        return subtitle, 1

    words = subtitle.split()
    if len(words) <= 1:
        return subtitle, 1

    best_split = min(
        ((" ".join(words[:i]), " ".join(words[i:])) for i in range(1, len(words))),
        key=lambda lines: abs(text_width_units(lines[0]) - text_width_units(lines[1])),
    )
    return "\n".join(best_split), 2


def top_line_gap(title_size, subtitle_size):
    return max(0, int(round(max(title_size, subtitle_size) * 0.35)))


def subtitle_internal_gap(subtitle_size):
    return max(0, int(round(subtitle_size * 0.15)))


def subtitle_block_height(subtitle_size, line_count):
    line_count = max(1, int(line_count))
    return subtitle_size * line_count + subtitle_internal_gap(subtitle_size) * (line_count - 1)


def fit_top_font_sizes(
    title,
    subtitle,
    requested_title_size,
    requested_subtitle_size,
    max_width,
    max_height,
    subtitle_line_count=1,
):
    title_width_limited = int(max_width / text_width_units(title))
    subtitle_width_limited = int(max_width / max_line_width_units(subtitle))
    title_size = max(1, min(int(requested_title_size), title_width_limited))
    subtitle_size = max(1, min(int(requested_subtitle_size), subtitle_width_limited))

    total_h = title_size + subtitle_block_height(subtitle_size, subtitle_line_count) + top_line_gap(title_size, subtitle_size)
    if total_h > max_height:
        scale = max_height / total_h
        title_size = max(1, int(title_size * scale))
        subtitle_size = max(1, int(subtitle_size * scale))
    return title_size, subtitle_size


def resolve_top(data):
    data = normalize_keys(data)
    h = int_value(data, "height_px", None) if data.get("height_px") else int_from_pct(data.get("height_pct", "13.5"), CANVAS_H)
    margin_top = max(int_from_pct(data.get("top_margin_pct", "0"), h), 0)
    if data.get("bottom_margin_pct") not in (None, ""):
        margin_bottom = max(int_from_pct(data["bottom_margin_pct"], h), 0)
    else:
        margin_bottom = max(int_value(data, "bottom_margin_px", 5), 0)
    margin_x = int_from_pct(data.get("side_margin_pct", "0"), CANVAS_W)
    usable = max(h - margin_top - margin_bottom, 1)
    requested_size = int_value(data, "size", usable * 0.36)
    requested_title_size = int_value(data, "title_size", requested_size)
    requested_subtitle_size = int_value(data, "subtitle_size", requested_size)
    max_text_w = max(CANVAS_W - margin_x * 2, 1)
    bottom_anchor_y = max(h - margin_bottom, margin_top + 1)
    max_text_h = usable
    subtitle, subtitle_line_count = wrap_subtitle_if_needed(
        data.get("subtitle", ""),
        requested_subtitle_size,
        max_text_w,
    )
    title_size, subtitle_size = fit_top_font_sizes(
        data.get("title", ""),
        subtitle,
        requested_title_size,
        requested_subtitle_size,
        max_text_w,
        max_text_h,
        subtitle_line_count,
    )
    gap = top_line_gap(title_size, subtitle_size)
    title_y = margin_top
    subtitle_y = title_y + title_size + gap
    x_expr = f"{margin_x}+((w-{margin_x * 2})-text_w)/2" if margin_x else "(w-text_w)/2"
    return {
        "height_px": h,
        "background": data.get("background", "black"),
        "title": data.get("title", ""),
        "subtitle": subtitle,
        "subtitle_line_count": subtitle_line_count,
        "font": data.get("font", "Noto Sans CJK KR"),
        "font_file": data.get("font_file", ""),
        "title_color": data.get("title_color", data.get("color", "white")),
        "subtitle_color": data.get("subtitle_color", data.get("color", "white")),
        "font_style": data.get("font_style", ""),
        "title_size": title_size,
        "subtitle_size": subtitle_size,
        "title_y": title_y,
        "subtitle_y": subtitle_y,
        "text_x": x_expr,
        "top_margin_pct": data.get("top_margin_pct", "0"),
        "bottom_margin_pct": data.get("bottom_margin_pct", ""),
        "side_margin_pct": data.get("side_margin_pct", "0"),
        "top_margin_px": margin_top,
        "bottom_margin_px": margin_bottom,
        "side_margin_px": margin_x,
        "bottom_anchor_y": bottom_anchor_y,
    }


def resolve_bottom(data):
    data = normalize_keys(data)
    h = int_value(data, "height_px", None) if data.get("height_px") else int_from_pct(data.get("height_pct", "18.75"), CANVAS_H)
    return {
        "height_px": h,
        "background": data.get("background", "black"),
    }


def emit_shell(values):
    for key, value in values.items():
        print(f"export {key}={shlex.quote(str(value))}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-file", required=True)
    parser.add_argument("--top-preset", default=os.environ.get("FRAME_TOP_PRESET", "default"))
    parser.add_argument("--bottom-file", required=True)
    parser.add_argument("--bottom-preset", default=os.environ.get("FRAME_BOTTOM_PRESET", "default"))
    parser.add_argument("--top-title")
    parser.add_argument("--top-subtitle")
    parser.add_argument("--top-height-pct")
    parser.add_argument("--bottom-height-pct")
    parser.add_argument("--top-margin-pct")
    parser.add_argument("--top-margin-x-pct")
    parser.add_argument("--shell", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    top = resolve(load_presets(Path(args.top_file)), args.top_preset)
    bottom = resolve(load_presets(Path(args.bottom_file)), args.bottom_preset)
    if args.top_title is not None:
        top["title"] = args.top_title
    if args.top_subtitle is not None:
        top["subtitle"] = args.top_subtitle
    if args.top_height_pct is not None:
        top["height_pct"] = args.top_height_pct
        top.pop("height_px", None)
    if args.bottom_height_pct is not None:
        bottom["height_pct"] = args.bottom_height_pct
        bottom.pop("height_px", None)
    if args.top_margin_pct is not None:
        top["top_margin_pct"] = args.top_margin_pct
    if args.top_margin_x_pct is not None:
        top["side_margin_pct"] = args.top_margin_x_pct

    top_resolved = resolve_top(top)
    bottom_resolved = resolve_bottom(bottom)
    content_h = CANVAS_H - top_resolved["height_px"] - bottom_resolved["height_px"]
    if content_h <= 0:
        raise SystemExit("top and bottom frame heights must leave positive B-roll content height")

    flat = {
        "FRAME_TOP_H": top_resolved["height_px"],
        "FRAME_BOTTOM_H": bottom_resolved["height_px"],
        "FRAME_TOP_BACKGROUND": top_resolved["background"],
        "FRAME_BOTTOM_BACKGROUND": bottom_resolved["background"],
        "FRAME_TOP_TITLE": top_resolved["title"],
        "FRAME_TOP_SUBTITLE": top_resolved["subtitle"],
        "FRAME_TOP_FONT_NAME": top_resolved["font"],
        "FRAME_TOP_FONT_FILE": top_resolved["font_file"],
        "FRAME_TOP_TITLE_COLOR": top_resolved["title_color"],
        "FRAME_TOP_SUBTITLE_COLOR": top_resolved["subtitle_color"],
        "FRAME_TOP_FONT_STYLE": top_resolved["font_style"],
        "FRAME_TOP_TITLE_SIZE": top_resolved["title_size"],
        "FRAME_TOP_SUBTITLE_SIZE": top_resolved["subtitle_size"],
        "FRAME_TOP_TITLE_Y": top_resolved["title_y"],
        "FRAME_TOP_SUBTITLE_Y": top_resolved["subtitle_y"],
        "FRAME_TOP_TEXT_X": top_resolved["text_x"],
        "FRAME_CONTENT_H": content_h,
        "FRAME_JSON": json.dumps({"top": top_resolved, "bottom": bottom_resolved, "content_h": content_h}, ensure_ascii=False),
    }
    if args.shell:
        emit_shell(flat)
    if args.json:
        print(flat["FRAME_JSON"])


if __name__ == "__main__":
    main()
