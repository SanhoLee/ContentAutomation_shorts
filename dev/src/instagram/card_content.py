"""Select Instagram carousel card texts from an already-generated job.

Reuses `video_meta.json`/`scenes.json` written by the script-generation stage
(`common/0_script.py`) — no new Claude call, so this stays free to run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def load_video_meta(work_dir: Path) -> dict[str, Any]:
    path = Path(work_dir) / "video_meta.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def load_scenes(work_dir: Path) -> list[dict[str, Any]]:
    path = Path(work_dir) / "scenes.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def truncate_for_card(text: str, max_chars: int = 80) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"


def _hook_text(video_meta: dict[str, Any]) -> str:
    title = str(video_meta.get("title") or "").strip()
    if title:
        return title
    frame_header = video_meta.get("frame_header") or {}
    return str(frame_header.get("title") or "").strip()


def _cta_text(video_meta: dict[str, Any]) -> str:
    final_answer = str(video_meta.get("final_answer") or "").strip()
    if final_answer:
        return final_answer
    return str(video_meta.get("core_message") or "").strip()


def select_card_texts(
    video_meta: dict[str, Any], scenes: list[dict[str, Any]], max_cards: int = 8
) -> list[str]:
    hook = _hook_text(video_meta)
    cta = _cta_text(video_meta)

    texts: list[str] = []
    if hook:
        texts.append(truncate_for_card(hook))

    slots_for_scenes = max_cards - len(texts) - (1 if cta else 0)
    for scene in scenes[: max(0, slots_for_scenes)]:
        text = str(scene.get("text") or "").strip()
        if text:
            texts.append(truncate_for_card(text))

    if cta:
        texts.append(truncate_for_card(cta))

    return texts[:max_cards]


def build_card_set(work_dir: Path, max_cards: int = 8) -> list[str]:
    video_meta = load_video_meta(work_dir)
    scenes = load_scenes(work_dir)
    return select_card_texts(video_meta, scenes, max_cards=max_cards)
