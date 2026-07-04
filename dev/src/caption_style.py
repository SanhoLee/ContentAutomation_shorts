#!/usr/bin/env python3
"""Build ffmpeg/libass force_style strings from editable caption style presets."""
import argparse
import json
import os
from pathlib import Path


STYLE_ORDER = [
    "FontName", "FontSize", "PrimaryColour", "SecondaryColour", "OutlineColour", "BackColour",
    "Bold", "Italic", "Underline", "StrikeOut", "ScaleX", "ScaleY", "Spacing", "Angle",
    "BorderStyle", "Outline", "Shadow", "Alignment", "MarginL", "MarginR", "MarginV", "WrapStyle",
]

ALIASES = {
    "font_name": "FontName", "font_size": "FontSize", "primary_colour": "PrimaryColour",
    "secondary_colour": "SecondaryColour", "outline_colour": "OutlineColour", "back_colour": "BackColour",
    "border_style": "BorderStyle", "outline": "Outline", "shadow": "Shadow", "alignment": "Alignment",
    "margin_l": "MarginL", "margin_r": "MarginR", "margin_v": "MarginV", "wrap_style": "WrapStyle",
}


def _parse_scalar(value: str):
    value = value.strip()
    if not value:
        return ""
    if (value.startswith("\"") and value.endswith("\"")) or (value.startswith("'") and value.endswith("'")):
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
        raise SystemExit(f"caption style preset not found in {path}")
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
        raise SystemExit(f"unknown caption style '{name}'. available: {available}")
    if name in seen:
        raise SystemExit(f"caption style inheritance cycle: {' -> '.join(seen + [name])}")
    raw = presets[name] or {}
    style = {}
    parent = raw.get("extends")
    if parent:
        style.update(resolve_style(presets, str(parent), seen + [name]))
    style.update(normalize_style({k: v for k, v in raw.items() if k != "extends"}))
    return style


def build_force_style(style: dict) -> str:
    keys = [key for key in STYLE_ORDER if key in style]
    keys += sorted(key for key in style if key not in STYLE_ORDER)
    return ",".join(f"{key}={style[key]}" for key in keys)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset-file", required=True)
    parser.add_argument("--style", default=os.environ.get("CAPTION_STYLE", "default"))
    parser.add_argument("--font-size")
    parser.add_argument("--margin-v")
    parser.add_argument("--margin-h")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    presets = load_presets(Path(args.preset_file))
    style = resolve_style(presets, args.style)
    if args.font_size:
        style["FontSize"] = str(args.font_size)
    if args.margin_v:
        style["MarginV"] = str(args.margin_v)
    if args.margin_h:
        style["MarginL"] = str(args.margin_h)
        style["MarginR"] = str(args.margin_h)

    force_style = build_force_style(style)
    if args.json:
        print(json.dumps({"style": args.style, "force_style": force_style, "values": style}, ensure_ascii=False))
    else:
        print(force_style)


if __name__ == "__main__":
    main()
