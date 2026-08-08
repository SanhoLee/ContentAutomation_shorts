#!/usr/bin/env python3
"""Build ffmpeg/libass subtitle styles and optional positioned ASS files from presets."""
import argparse
import json
import os
import re
from pathlib import Path

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
STYLE_ORDER = [
    "FontName", "FontSize", "PrimaryColour", "SecondaryColour", "OutlineColour", "BackColour",
    "Bold", "Italic", "Underline", "StrikeOut", "ScaleX", "ScaleY", "Spacing", "Angle",
    "BorderStyle", "Outline", "Shadow", "Alignment", "MarginL", "MarginR", "MarginV", "WrapStyle",
]
ASS_STYLE_FIELDS = [
    "Name", "Fontname", "Fontsize", "PrimaryColour", "SecondaryColour", "OutlineColour", "BackColour",
    "Bold", "Italic", "Underline", "StrikeOut", "ScaleX", "ScaleY", "Spacing", "Angle",
    "BorderStyle", "Outline", "Shadow", "Alignment", "MarginL", "MarginR", "MarginV", "Encoding",
]
POSITION_KEYS = {"PositionMode", "PosX", "PosY", "OffsetX", "OffsetY", "Anchor", "PlayResX", "PlayResY"}
ALIASES = {
    "font_name": "FontName", "font_size": "FontSize", "primary_colour": "PrimaryColour",
    "secondary_colour": "SecondaryColour", "outline_colour": "OutlineColour", "back_colour": "BackColour",
    "border_style": "BorderStyle", "outline": "Outline", "shadow": "Shadow", "alignment": "Alignment",
    "margin_l": "MarginL", "margin_r": "MarginR", "margin_v": "MarginV", "wrap_style": "WrapStyle",
    "position_mode": "PositionMode", "pos_x": "PosX", "pos_y": "PosY", "offset_x": "OffsetX",
    "offset_y": "OffsetY", "anchor": "Anchor", "play_res_x": "PlayResX", "play_res_y": "PlayResY",
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
    keys += sorted(key for key in style if key not in STYLE_ORDER and key not in POSITION_KEYS)
    return ",".join(f"{key}={style[key]}" for key in keys)


def _int_value(style: dict, key: str, default: int) -> int:
    try:
        return int(float(style.get(key, default)))
    except ValueError:
        raise SystemExit(f"{key} must be numeric: {style.get(key)}") from None


def position_override(style: dict) -> str:
    mode = str(style.get("PositionMode", "")).strip().lower()
    if mode in ("", "alignment", "margin"):
        return ""
    play_x = _int_value(style, "PlayResX", VIDEO_WIDTH)
    play_y = _int_value(style, "PlayResY", VIDEO_HEIGHT)
    anchor = _int_value(style, "Anchor", _int_value(style, "Alignment", 5))
    if mode in ("absolute", "pos", "position"):
        x = _int_value(style, "PosX", play_x // 2)
        y = _int_value(style, "PosY", play_y // 2)
    elif mode in ("center-offset", "offset", "center_offset"):
        x = play_x // 2 + _int_value(style, "OffsetX", 0)
        y = play_y // 2 + _int_value(style, "OffsetY", 0)
    else:
        raise SystemExit(f"unknown PositionMode: {style.get('PositionMode')}")
    return f"{{\\an{anchor}\\pos({x},{y})}}"


def ass_style(style: dict) -> str:
    values = {
        "Name": "Default",
        "Fontname": style.get("FontName", "Noto Sans CJK KR"),
        "Fontsize": style.get("FontSize", "22"),
        "PrimaryColour": style.get("PrimaryColour", "&H00FFFFFF"),
        "SecondaryColour": style.get("SecondaryColour", "&H000000FF"),
        "OutlineColour": style.get("OutlineColour", "&H00000000"),
        "BackColour": style.get("BackColour", "&H80000000"),
        "Bold": style.get("Bold", "0"),
        "Italic": style.get("Italic", "0"),
        "Underline": style.get("Underline", "0"),
        "StrikeOut": style.get("StrikeOut", "0"),
        "ScaleX": style.get("ScaleX", "100"),
        "ScaleY": style.get("ScaleY", "100"),
        "Spacing": style.get("Spacing", "0"),
        "Angle": style.get("Angle", "0"),
        "BorderStyle": style.get("BorderStyle", "1"),
        "Outline": style.get("Outline", "2"),
        "Shadow": style.get("Shadow", "0"),
        "Alignment": style.get("Alignment", style.get("Anchor", "2")),
        "MarginL": style.get("MarginL", "10"),
        "MarginR": style.get("MarginR", "10"),
        "MarginV": style.get("MarginV", "60"),
        "Encoding": style.get("Encoding", "1"),
    }
    return "Style: " + ",".join(str(values[field]) for field in ASS_STYLE_FIELDS)


def _srt_time_to_ass(value: str) -> str:
    value = value.strip().replace(",", ".")
    h, m, rest = value.split(":")
    seconds, centis = rest.split(".")
    return f"{int(h)}:{m}:{seconds}.{centis[:2].ljust(2, '0')}"


def parse_srt(path: Path):
    text = path.read_text(encoding="utf-8-sig")
    blocks = re.split(r"\n\s*\n", text.strip())
    for block in blocks:
        lines = [line.rstrip("\r") for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        if "-->" in lines[0]:
            time_line = lines[0]
            content = lines[1:]
        else:
            time_line = lines[1]
            content = lines[2:]
        if "-->" not in time_line or not content:
            continue
        start, end = [part.strip().split()[0] for part in time_line.split("-->", 1)]
        yield _srt_time_to_ass(start), _srt_time_to_ass(end), r"\N".join(content)


def escape_ass_text(text: str) -> str:
    return text.replace("{", "（").replace("}", "）")


def write_ass(srt_in: Path, ass_out: Path, style: dict):
    play_x = _int_value(style, "PlayResX", VIDEO_WIDTH)
    play_y = _int_value(style, "PlayResY", VIDEO_HEIGHT)
    override = position_override(style)
    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {play_x}",
        f"PlayResY: {play_y}",
        "ScaledBorderAndShadow: yes",
        "WrapStyle: " + str(style.get("WrapStyle", "1")),
        "",
        "[V4+ Styles]",
        "Format: " + ",".join(ASS_STYLE_FIELDS),
        ass_style(style),
        "",
        "[Events]",
        "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    for start, end, text in parse_srt(srt_in):
        lines.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{override}{escape_ass_text(text)}")
    ass_out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preset-file", required=True)
    parser.add_argument("--style", default=os.environ.get("CAPTION_STYLE", "default"))
    parser.add_argument("--font-size")
    parser.add_argument("--margin-v")
    parser.add_argument("--margin-h")
    parser.add_argument("--offset-x")
    parser.add_argument("--offset-y")
    parser.add_argument("--pos-x")
    parser.add_argument("--pos-y")
    parser.add_argument("--srt-in")
    parser.add_argument("--ass-out")
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
    if args.offset_x:
        style["PositionMode"] = "center-offset"
        style["OffsetX"] = str(args.offset_x)
    if args.offset_y:
        style["PositionMode"] = "center-offset"
        style["OffsetY"] = str(args.offset_y)
    if args.pos_x:
        style["PositionMode"] = "absolute"
        style["PosX"] = str(args.pos_x)
    if args.pos_y:
        style["PositionMode"] = "absolute"
        style["PosY"] = str(args.pos_y)

    force_style = build_force_style(style)
    position = position_override(style)
    ass_path = None
    if args.srt_in or args.ass_out:
        if not args.srt_in or not args.ass_out:
            raise SystemExit("--srt-in and --ass-out must be used together")
        ass_path = Path(args.ass_out)
        write_ass(Path(args.srt_in), ass_path, style)

    if args.json:
        print(json.dumps({
            "style": args.style,
            "force_style": force_style,
            "position_override": position,
            "ass_path": str(ass_path) if ass_path else None,
            "values": style,
        }, ensure_ascii=False))
    else:
        print(force_style)


if __name__ == "__main__":
    main()
