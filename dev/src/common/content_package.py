"""content_package.py — normalize one job's finished script into a single
cross-platform JSON contract (Phase 3 of the Pareto design spec).

Downstream adapters (X thread today; Instagram/WordPress later) read
`content_package.json` instead of re-deriving structure from
script.txt/scenes.json/video_meta.json each time. strategy.json, script.txt,
scenes.json and video_meta.json are read-only sources here — this module
never writes to them.

No additional Claude calls: scene roles are a position heuristic over
already-generated scenes, and evidence comes straight from Stage 1's
evidence_brief (already computed, zero extra cost).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

MIN_KEY_POINTS = 3
MAX_KEY_POINTS = 7

DEFAULT_PLATFORMS = {
    "youtube_shorts": {"ready": True},
    "x_thread": {"ready": False},
    "instagram_carousel": {"ready": False},
    "wordpress": {"ready": False},
}


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def _scene_role(index: int, total: int) -> str:
    """Position heuristic, no extra Claude call: first scene is the hook,
    last is the CTA, everything between alternates principle/example.

    Only used for jobs whose scenes carry no `role` of their own — i.e. runs
    with USE_STORY_TYPES off, or scripts written before story types existed.
    """
    if total <= 1 or index == 0:
        return "hook"
    if index == total - 1:
        return "cta"
    return "principle" if index % 2 == 1 else "example"


def _scene_visual(scene: dict[str, Any]) -> dict[str, Any] | None:
    visual = scene.get("visual")
    if not isinstance(visual, dict):
        return None
    must_show = visual.get("must_show")
    return {
        "type": str(visual.get("type") or ""),
        "brief": str(visual.get("brief") or ""),
        "must_show": [str(item) for item in must_show] if isinstance(must_show, list) else [],
    }


def _build_scenes(raw_scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    total = len(raw_scenes)
    scenes = []
    for i, scene in enumerate(raw_scenes):
        # story_type runs write their own role from the genre's sequence; keep
        # it rather than overwriting it with the position guess.
        role = str(scene.get("role") or "").strip() or _scene_role(i, total)
        entry = {
            "index": i + 1,
            "role": role,
            "text": str(scene.get("text", "")),
            "visual_query": str(scene.get("visual_query", "")),
        }
        visual = _scene_visual(scene)
        if visual is not None:
            entry["visual"] = visual
        scenes.append(entry)
    return scenes


def _build_key_points(scenes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    body = [s for s in scenes if s["role"] in ("principle", "example")]
    if len(body) < MIN_KEY_POINTS:
        # Widen the pool but never re-include hook/cta -- both are already
        # surfaced separately (package["hook"] / package["cta"]), so folding
        # them back in here would duplicate them for very short scripts. Only
        # fall all the way back to every scene if nothing else is left.
        body = [s for s in scenes if s["role"] not in ("hook", "cta")] or body or scenes
    return [{"id": s["index"], "text": s["text"], "evidence_ref": None} for s in body[:MAX_KEY_POINTS]]


def _build_evidence(strategy: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = []
    for item in strategy.get("evidence_brief") or ():
        claim = str((item or {}).get("claim") or "").strip()
        if not claim:
            continue
        evidence.append({
            "claim": claim,
            "source_hint": str(item.get("source") or "").strip(),
            "year": item.get("year") or None,
        })
    return evidence


def _split_hashtags(raw: str) -> list[str]:
    return [tag for tag in str(raw or "").split() if tag.startswith("#")]


def build_content_package(
    job_dir: str | Path, *, niche: str = "brain_health", locale: str = "ko-KR",
) -> dict[str, Any] | None:
    """Build/refresh content_package.json for one job directory.

    Returns the built payload, or None if the job has no script yet (Stage 2
    has not written scenes.json/video_meta.json). Never raises: a partially
    written job directory yields a smaller package rather than an exception,
    so a caller like 0_script.write_outputs can call this without risking the
    core pipeline over a downstream-only artifact.
    """
    job_dir = Path(job_dir)
    video_meta = _read_json(job_dir / "video_meta.json", None)
    raw_scenes = _read_json(job_dir / "scenes.json", None)
    if video_meta is None or raw_scenes is None:
        return None

    strategy = _read_json(job_dir / "strategy.json", {})
    scenes = _build_scenes(raw_scenes)
    existing = _read_json(job_dir / "content_package.json", None)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    package = {
        "job_id": job_dir.name,
        "version": SCHEMA_VERSION,
        "locale": locale,
        "niche": niche,
        "source": {
            "topic": video_meta.get("topic", ""),
            "main_keyword": video_meta.get("main_keyword", ""),
            "hook_type": video_meta.get("hook_type", ""),
            "title": video_meta.get("title", ""),
        },
        # Empty string, not absent, when story types are off: adapters can then
        # read package["story_type"] unconditionally.
        "story_type": str(video_meta.get("story_type") or strategy.get("story_type") or ""),
        "format_type": str(
            video_meta.get("format_type")
            or strategy.get("format_type")
            or (strategy.get("content_design") or {}).get("format_type")
            or ""
        ),
        "hook": scenes[0]["text"] if scenes else "",
        "core_message": video_meta.get("core_message", ""),
        "key_points": _build_key_points(scenes),
        "evidence": _build_evidence(strategy),
        "scenes": scenes,
        "cta": {
            "action": scenes[-1]["text"] if scenes else "",
            "next_topic_tease": str(strategy.get("cta_next") or ""),
        },
        "hashtags": _split_hashtags(video_meta.get("hashtags", "")),
        "platforms": (existing or {}).get("platforms") or dict(DEFAULT_PLATFORMS),
        "created_at": (existing or {}).get("created_at") or now,
        "updated_at": now,
    }
    (job_dir / "content_package.json").write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return package


def load_content_package(job_dir: str | Path) -> dict[str, Any] | None:
    return _read_json(Path(job_dir) / "content_package.json", None)


def update_platform_flag(job_dir: str | Path, platform: str, ready: bool) -> dict[str, Any] | None:
    """Flip platforms.<platform>.ready, e.g. after an adapter writes its output.

    Returns None (no-op) if content_package.json does not exist yet.
    """
    job_dir = Path(job_dir)
    package = load_content_package(job_dir)
    if package is None:
        return None
    platforms = package.setdefault("platforms", {})
    entry = dict(platforms.get(platform) or {})
    entry["ready"] = bool(ready)
    platforms[platform] = entry
    package["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    (job_dir / "content_package.json").write_text(
        json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return package
