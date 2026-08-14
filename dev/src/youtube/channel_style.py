#!/usr/bin/env python3
"""Resolve channel-name/watermark drawtext placement presets (zone-based, same vocabulary as caption_style.py)."""
import argparse
import json
import os
import shlex
from pathlib import Path

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
ALIASES = {
    "font_name": "FontName", "font_file": "FontFile", "font_style": "FontStyle",
    "font_size": "FontSize", "color": "Color", "text": "Text",
    "zone": "Zone", "x_pct": "XPct", "valign": "VAlign", "distance_pct": "DistancePct",
    "zone_top_h": "ZoneTopH", "zone_bottom_h": "ZoneBottomH", "play_res_x": "PlayResX", "play_res_y": "PlayResY",
}


def _parse_scalar(value: str):
    value = value.strip()
    if not value:
        return ""
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def load_presets(path: Path) -> dict:
    presets = {}
    in_presets = False
    current = None
    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.split("#", 1)[0].rstrip("\n")
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
                continue
            if indent >= 4 and current and ":" in stripped:
                key, value = stripped.split(":", 1)
                presets[current][key.strip()] = _parse_scalar(value)
    if not presets:
        raise SystemExit(f"channel style preset not found in {path}")
    return presets


def normalize_style(style: dict) -> dict:
    normalized = {}
    for key, value in (style or {}).items():
        style_key = ALIASES.get(str(key), str(key))
        if value is not None:
            normalized[style_key] = str(value)
    return normalized


def resolve_style(presets: dict, name: str, seen=None) -> dict:
    seen = seen or []
    if name not in presets:
        available = ", ".join(sorted(presets))
        raise SystemExit(f"unknown channel style '{name}'. available: {available}")
    if name in seen:
        raise SystemExit(f"channel style inheritance cycle: {' -> '.join(seen + [name])}")
    raw = presets[name] or {}
    style = {}
    parent = raw.get("extends")
    if parent:
        style.update(resolve_style(presets, str(parent), seen + [name]))
    style.update(normalize_style({k: v for k, v in raw.items() if k != "extends"}))
    return style


def _int_value(style: dict, key: str, default: int) -> int:
    try:
        return int(float(style.get(key, default)))
    except ValueError:
        raise SystemExit(f"{key} must be numeric: {style.get(key)}") from None


def zone_bounds(style: dict) -> tuple:
    """(top_y, bottom_y) in canvas pixels for the named zone, given the framed-mode bar heights."""
    zone = str(style.get("Zone", "broll")).strip().lower()
    play_y = _int_value(style, "PlayResY", VIDEO_HEIGHT)
    top_h = _int_value(style, "ZoneTopH", 0)
    bottom_h = _int_value(style, "ZoneBottomH", 0)
    if zone == "frame_top":
        top, bottom = 0, top_h
    elif zone == "frame_bottom":
        top, bottom = play_y - bottom_h, play_y
    elif zone == "broll":
        top, bottom = top_h, play_y - bottom_h
    else:
        raise SystemExit(f"unknown Zone: {style.get('Zone')} (expected frame_top, frame_bottom, or broll)")
    if bottom - top <= 0:
        raise SystemExit(
            f"Zone '{zone}' has no height (top={top}, bottom={bottom}); "
            "frame_top/frame_bottom require FRAME_MODE=framed with a nonzero bar height"
        )
    return top, bottom


def position_exprs(style: dict) -> tuple:
    """ffmpeg drawtext x/y expression strings. drawtext anchors from the text box's top-left
    corner (unlike ASS \\pos), so position must be expressed with text_w/text_h at render time."""
    zone_top, zone_bottom = zone_bounds(style)
    zone_h = zone_bottom - zone_top
    play_x = _int_value(style, "PlayResX", VIDEO_WIDTH)
    x_pct = _int_value(style, "XPct", 50)
    distance_pct = _int_value(style, "DistancePct", 0)
    x_expr = f"({play_x}*{x_pct}/100)-text_w/2"
    valign = str(style.get("VAlign", "bottom")).strip().lower()
    if valign == "top":
        y_expr = f"({zone_top}+({zone_h})*{distance_pct}/100)"
    elif valign == "bottom":
        y_expr = f"({zone_bottom}-({zone_h})*{distance_pct}/100-text_h)"
    else:
        raise SystemExit(f"unknown VAlign: {style.get('VAlign')} (expected top or bottom)")
    return x_expr, y_expr


def emit_shell(values):
    for key, value in values.items():
        print(f"export {key}={shlex.quote(str(value))}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset-file", required=True)
    parser.add_argument("--style", default=os.environ.get("CHANNEL_STYLE", "default"))
    parser.add_argument("--text")
    parser.add_argument("--zone-top-h", default="0")
    parser.add_argument("--zone-bottom-h", default="0")
    parser.add_argument("--zone")
    parser.add_argument("--x-pct")
    parser.add_argument("--valign")
    parser.add_argument("--distance-pct")
    parser.add_argument("--shell", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    presets = load_presets(Path(args.preset_file))
    style = resolve_style(presets, args.style)
    if args.text is not None:
        style["Text"] = str(args.text)
    style["ZoneTopH"] = str(args.zone_top_h)
    style["ZoneBottomH"] = str(args.zone_bottom_h)
    if args.zone:
        style["Zone"] = str(args.zone)
    if args.x_pct:
        style["XPct"] = str(args.x_pct)
    if args.valign:
        style["VAlign"] = str(args.valign)
    if args.distance_pct:
        style["DistancePct"] = str(args.distance_pct)

    text = style.get("Text", "")
    x_expr, y_expr = ("0", "0")
    if text:
        x_expr, y_expr = position_exprs(style)

    flat = {
        "CHANNEL_TEXT": text,
        "CHANNEL_FONT_NAME": style.get("FontName", "Noto Sans CJK KR"),
        "CHANNEL_FONT_FILE": style.get("FontFile", ""),
        "CHANNEL_FONT_STYLE": style.get("FontStyle", ""),
        "CHANNEL_COLOR": style.get("Color", "white"),
        "CHANNEL_FONT_SIZE": style.get("FontSize", "60"),
        "CHANNEL_X_EXPR": x_expr,
        "CHANNEL_Y_EXPR": y_expr,
    }
    if args.shell:
        emit_shell(flat)
    if args.json:
        print(json.dumps(flat, ensure_ascii=False))


if __name__ == "__main__":
    main()
