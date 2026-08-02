#!/usr/bin/env python3
"""x_photo_card.py — content_package.json -> lead photo for an X thread.

Reuses `dev/src/instagram/card_render.py`'s Pillow + Noto Sans CJK KR text
card renderer instead of building a second image pipeline. Rendered at a
16:9 canvas (X's timeline card aspect) rather than Instagram's 4:5 portrait.

Rendering failure (missing font, bad Pillow install, etc.) returns None --
the caller posts the first tweet as text-only rather than failing the
thread over a decorative image.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ADAPTERS_DIR = Path(__file__).resolve().parent
COMMON_DIR = ADAPTERS_DIR.parent
SRC_DIR = COMMON_DIR.parent
INSTAGRAM_DIR = SRC_DIR / "instagram"
for _path in (COMMON_DIR, INSTAGRAM_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

THREAD_PHOTO_SIZE = (1200, 675)
PHOTO_FILENAME = "x_thread_photo.png"


def _video_meta_title(job_dir: Path) -> str:
    path = job_dir / "video_meta.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str((data or {}).get("title") or "").strip()


def _photo_text(package: dict[str, Any], job_dir: Path) -> str:
    hook = str(package.get("hook") or "").strip()
    if hook:
        return hook
    return _video_meta_title(job_dir)


def build_thread_photo(package: dict[str, Any], job_dir: str | Path) -> Path | None:
    job_dir = Path(job_dir)
    text = _photo_text(package, job_dir)
    if not text:
        return None
    output_path = job_dir / PHOTO_FILENAME
    try:
        # Imported here, not at module scope: card_render pulls in Pillow, and
        # x_thread_adapter imports this module. A host without Pillow must
        # lose the card, not the whole thread stage and the bots with it.
        import card_render
    except ImportError as exc:
        print(f"Pillow가 없어 X 스레드 사진을 건너뜁니다: {exc}", file=sys.stderr)
        return None
    try:
        return card_render.render_card(text, output_path, canvas_size=THREAD_PHOTO_SIZE)
    except (OSError, ValueError) as exc:
        print(f"X 스레드 사진 렌더링 실패, 사진 없이 진행합니다: {exc}", file=sys.stderr)
        return None
