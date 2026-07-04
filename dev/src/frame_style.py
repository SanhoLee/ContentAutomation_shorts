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
    "height_pct": "height_pct",
    "bg_color": "bg_color",
    "title": "title",
    "subtitle": "subtitle",
    "font_name": "font_name",
    "font_file": "font_file",
    "font_color": "font_color",
    "font_size": "font_size",
    "title_font_size": "title_font_size",
    "subtitle_font_size": "subtitle_font_size",
    "margin_y": "margin_y",
    "margin_top_pct": "margin_top_pct",
    "margin_x_pct": "margin_x_pct",
    "channel_name": "channel_name",
    "channel_font_name": "channel_font_name",
    "channel_font_file": "channel_font_file",
    "channel_font_color": "channel_font_color",
    "channel_font_size": "channel_font_size",
    "channel_margin_top": "channel_margin_top",
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


def fit_equal_font_size(title, subtitle, requested_size, max_width, max_height):
    width_units = max(text_width_units(title), text_width_units(subtitle))
    width_limited = int(max_width / width_units)
    # Two equal-height text lines plus an inter-line gap around 35% of the font size.
    height_limited = int(max_height / 2.35)
    return max(1, min(int(requested_size), width_limited, height_limited))


def resolve_top(data):
    h = int_value(data, "height_px", None) if data.get("height_px") else int_from_pct(data.get("height_pct", "13.5"), CANVAS_H)
    margin = int_value(data, "margin_y", 5)
    margin_top = int_from_pct(data.get("margin_top_pct", "0"), h)
    margin_x = int_from_pct(data.get("margin_x_pct", "0"), CANVAS_W)
    usable = max(h - margin * 2, 1)
    requested_size = int_value(data, "font_size", usable * 0.36)
    if data.get("title_font_size") or data.get("subtitle_font_size"):
        requested_size = min(
            int_value(data, "title_font_size", requested_size),
            int_value(data, "subtitle_font_size", requested_size),
        )
    max_text_w = max(CANVAS_W - margin_x * 2, 1)
    text_top = margin + margin_top
    max_text_h = max(h - text_top - margin, 1)
    font_size = fit_equal_font_size(data.get("title", ""), data.get("subtitle", ""), requested_size, max_text_w, max_text_h)
    title_size = font_size
    subtitle_size = font_size
    gap = max(0, int(round(font_size * 0.35)))
    total_text_h = title_size + subtitle_size + gap
    title_y = min(max(text_top, margin), max(h - total_text_h - margin, margin))
    subtitle_y = title_y + title_size + gap
    x_expr = f"{margin_x}+((w-{margin_x * 2})-text_w)/2" if margin_x else "(w-text_w)/2"
    return {
        "height_px": h,
        "bg_color": data.get("bg_color", "black"),
        "title": data.get("title", ""),
        "subtitle": data.get("subtitle", ""),
        "font_name": data.get("font_name", "Noto Sans CJK KR"),
        "font_file": data.get("font_file", ""),
        "font_color": data.get("font_color", "white"),
        "title_font_size": title_size,
        "subtitle_font_size": subtitle_size,
        "title_y": title_y,
        "subtitle_y": subtitle_y,
        "text_x": x_expr,
        "margin_y": margin,
        "margin_top_pct": data.get("margin_top_pct", "0"),
        "margin_x_pct": data.get("margin_x_pct", "0"),
        "margin_top_px": margin_top,
        "margin_x_px": margin_x,
    }


def resolve_bottom(data):
    h = int_value(data, "height_px", None) if data.get("height_px") else int_from_pct(data.get("height_pct", "18.75"), CANVAS_H)
    margin_top = int_value(data, "channel_margin_top", 10)
    return {
        "height_px": h,
        "bg_color": data.get("bg_color", "black"),
        "channel_name": data.get("channel_name", "브레인피프티"),
        "channel_font_name": data.get("channel_font_name", data.get("font_name", "Noto Sans CJK KR")),
        "channel_font_file": data.get("channel_font_file", data.get("font_file", "")),
        "channel_font_color": data.get("channel_font_color", data.get("font_color", "white")),
        "channel_font_size": int_value(data, "channel_font_size", min(max(h * 0.16, 42), 72)),
        "channel_margin_top": margin_top,
        "channel_y": CANVAS_H - h + margin_top,
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
    parser.add_argument("--channel-name")
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
    if args.channel_name is not None:
        bottom["channel_name"] = args.channel_name
    if args.top_height_pct is not None:
        top["height_pct"] = args.top_height_pct
        top.pop("height_px", None)
    if args.bottom_height_pct is not None:
        bottom["height_pct"] = args.bottom_height_pct
        bottom.pop("height_px", None)
    if args.top_margin_pct is not None:
        top["margin_top_pct"] = args.top_margin_pct
    if args.top_margin_x_pct is not None:
        top["margin_x_pct"] = args.top_margin_x_pct

    top_resolved = resolve_top(top)
    bottom_resolved = resolve_bottom(bottom)
    content_h = CANVAS_H - top_resolved["height_px"] - bottom_resolved["height_px"]
    if content_h <= 0:
        raise SystemExit("top and bottom frame heights must leave positive B-roll content height")

    flat = {
        "FRAME_TOP_H": top_resolved["height_px"],
        "FRAME_BOTTOM_H": bottom_resolved["height_px"],
        "FRAME_BG_COLOR": top_resolved["bg_color"],
        "FRAME_TOP_TITLE": top_resolved["title"],
        "FRAME_TOP_SUBTITLE": top_resolved["subtitle"],
        "FRAME_TOP_FONT_NAME": top_resolved["font_name"],
        "FRAME_TOP_FONT_FILE": top_resolved["font_file"],
        "FRAME_TOP_FONT_COLOR": top_resolved["font_color"],
        "FRAME_TOP_TITLE_FONT_SIZE": top_resolved["title_font_size"],
        "FRAME_TOP_SUBTITLE_FONT_SIZE": top_resolved["subtitle_font_size"],
        "FRAME_TOP_TITLE_Y": top_resolved["title_y"],
        "FRAME_TOP_SUBTITLE_Y": top_resolved["subtitle_y"],
        "FRAME_TOP_TEXT_X": top_resolved["text_x"],
        "FRAME_BOTTOM_CHANNEL_NAME": bottom_resolved["channel_name"],
        "FRAME_BOTTOM_FONT_NAME": bottom_resolved["channel_font_name"],
        "FRAME_BOTTOM_FONT_FILE": bottom_resolved["channel_font_file"],
        "FRAME_BOTTOM_FONT_COLOR": bottom_resolved["channel_font_color"],
        "FRAME_BOTTOM_FONT_SIZE": bottom_resolved["channel_font_size"],
        "FRAME_BOTTOM_CHANNEL_Y": bottom_resolved["channel_y"],
        "FRAME_CONTENT_H": content_h,
        "FRAME_JSON": json.dumps({"top": top_resolved, "bottom": bottom_resolved, "content_h": content_h}, ensure_ascii=False),
    }
    if args.shell:
        emit_shell(flat)
    if args.json:
        print(flat["FRAME_JSON"])


if __name__ == "__main__":
    main()
