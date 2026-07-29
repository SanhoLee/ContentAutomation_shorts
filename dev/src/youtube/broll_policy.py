"""Aspect-aware Pexels B-roll selection and normalization policy."""
import json
import math
import os
import random
from pathlib import Path

TARGET_ASPECT = float(os.environ.get("BROLL_CONTENT_ASPECT", str(1080 / 1300)))
PORTRAIT_TARGET_RATIO = float(os.environ.get("BROLL_PORTRAIT_TARGET_RATIO", "0.60"))
MAX_ORIENTATION_STREAK = int(os.environ.get("BROLL_MAX_ORIENTATION_STREAK", "2"))

# Playback speed applied during normalization. Stock B-roll is shot to feel
# calm, which reads as sluggish under fast Shorts narration; a mild speed-up
# restores energy without looking like a comedy fast-forward.
PLAYBACK_SPEED = float(os.environ.get("BROLL_PLAYBACK_SPEED", "1.18"))
# Source clips much longer than a Shorts scene are almost always slow ambient
# footage (drifting clouds, static interiors). Shorter clips carry real motion.
IDEAL_SOURCE_DURATION = float(os.environ.get("BROLL_IDEAL_SOURCE_DURATION", "12"))
LONG_SOURCE_PENALTY_START = float(os.environ.get("BROLL_LONG_SOURCE_SECONDS", "25"))
# Clips reused across *previous jobs*, not just within this one. Without this
# every video redraws from the same handful of top-ranked Pexels results.
HISTORY_LIMIT = int(os.environ.get("BROLL_HISTORY_LIMIT", "300"))
CROSS_JOB_REUSE_PENALTY = float(os.environ.get("BROLL_CROSS_JOB_PENALTY", "55"))


def _default_history_path():
    work_dir = Path(os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work")))
    return work_dir.parent / "broll_usage.json"


def load_recent_video_ids(path=None, limit=HISTORY_LIMIT):
    """Video IDs used by recent jobs, newest first. Missing file returns []."""
    target = Path(path) if path else _default_history_path()
    if not target.is_file():
        return []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    entries = raw.get("recent") if isinstance(raw, dict) else raw
    if not isinstance(entries, list):
        return []
    return [item for item in entries if item is not None][:limit]


def record_used_video_ids(video_ids, path=None, limit=HISTORY_LIMIT):
    """Prepend this job's clip IDs so later jobs can avoid repeating them."""
    target = Path(path) if path else _default_history_path()
    fresh = [item for item in video_ids if item is not None]
    merged = list(dict.fromkeys(fresh + load_recent_video_ids(target, limit)))[:limit]
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({"recent": merged}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        # History is an optimization, never a reason to fail a render.
        pass
    return merged


def orientation(width, height):
    if not width or not height:
        return "unknown"
    ratio = float(width) / float(height)
    if ratio < 0.90:
        return "portrait"
    if ratio > 1.10:
        return "landscape"
    return "square"


def crop_retention(width, height, target_aspect=TARGET_ASPECT):
    if not width or not height or target_aspect <= 0:
        return 0.0
    source_aspect = float(width) / float(height)
    return min(source_aspect / target_aspect, target_aspect / source_aspect)


def choose_video_file(video):
    files = [f for f in video.get("video_files", []) if f.get("link") and f.get("width") and f.get("height")]
    if not files:
        return None

    def file_score(item):
        width, height = int(item["width"]), int(item["height"])
        short_edge = min(width, height)
        quality = min(short_edge / 720.0, 1.0) * 20
        # Prefer useful HD files without downloading unnecessarily huge masters.
        pixel_distance = abs(math.log(max(width * height, 1) / (1280 * 720)))
        # A 24fps master judders once sped up; 30/60fps renditions stay smooth.
        smoothness = min(float(item.get("fps") or 0) / 30.0, 2.0) * 3
        return quality - pixel_distance + smoothness

    return max(files, key=file_score)


def _portrait_ratio(history):
    directed = [item for item in history if item in ("portrait", "landscape")]
    if not directed:
        return 0.0
    return directed.count("portrait") / len(directed)


def score_candidate(
    video, file_info, min_duration, used_video_ids, orientation_history,
    jitter=0.0, recent_video_ids=(),
):
    width, height = int(file_info["width"]), int(file_info["height"])
    kind = orientation(width, height)
    duration = float(video.get("duration") or 0)
    score = crop_retention(width, height) * 45
    score += min(min(width, height) / 720.0, 1.0) * 12
    score += 90 if video.get("id") not in used_video_ids else -35
    score += 70 if duration >= float(min_duration) else -45
    # Penalize, never hard-block: a starved query must still return something.
    if video.get("id") in set(recent_video_ids):
        score -= CROSS_JOB_REUSE_PENALTY
    # Long stock clips are overwhelmingly slow ambient footage. Favor clips
    # near IDEAL_SOURCE_DURATION and taper off past LONG_SOURCE_PENALTY_START.
    if duration > LONG_SOURCE_PENALTY_START:
        score -= min((duration - LONG_SOURCE_PENALTY_START) * 0.8, 20)
    elif float(min_duration) <= duration <= IDEAL_SOURCE_DURATION:
        score += 10
    score += min(float(file_info.get("fps") or 0) / 30.0, 2.0) * 6

    portrait_ratio = _portrait_ratio(orientation_history)
    if kind == "portrait":
        score += 14 if portrait_ratio < PORTRAIT_TARGET_RATIO else -4
    elif kind == "landscape":
        score += 12 if portrait_ratio >= PORTRAIT_TARGET_RATIO else 2
    else:
        score += 8

    if len(orientation_history) >= MAX_ORIENTATION_STREAK and all(
        item == kind for item in orientation_history[-MAX_ORIENTATION_STREAK:]
    ):
        score -= 28
    return score + jitter


def select_video(
    videos, min_duration, used_video_ids, orientation_history, rng=None,
    recent_video_ids=(),
):
    rng = rng or random
    ranked = []
    for video in videos:
        file_info = choose_video_file(video)
        if not file_info:
            continue
        ranked.append((
            score_candidate(
                video, file_info, min_duration, used_video_ids, orientation_history,
                rng.random() * 1.5, recent_video_ids,
            ),
            video,
            file_info,
        ))
    if not ranked:
        return None
    _, video, file_info = max(ranked, key=lambda item: item[0])
    kind = orientation(file_info["width"], file_info["height"])
    retention = crop_retention(file_info["width"], file_info["height"])
    fit_mode = "blur-contain" if kind == "landscape" and retention < 0.88 else "cover"
    return {
        "video": video,
        "file": file_info,
        "orientation": kind,
        "fit_mode": fit_mode,
        "crop_retention": round(retention, 3),
        "duplicate_allowed": video.get("id") in used_video_ids,
        "short_allowed": float(video.get("duration") or 0) < float(min_duration),
    }


def normalization_filter(fit_mode, duration, fade_duration=0.3, speed=None):
    fade_out_start = max(float(duration) - fade_duration, 0)
    fades = f"fade=t=in:st=0:d={fade_duration},fade=t=out:st={fade_out_start}:d={fade_duration}"
    # setpts runs first so the speed-up applies to source timing; the caller's
    # `-t duration` still fixes output length (the input is stream-looped), and
    # the fades stay anchored to output time.
    speed = PLAYBACK_SPEED if speed is None else float(speed)
    pace = f"setpts=PTS/{speed:.4f}," if speed and abs(speed - 1.0) > 1e-3 else ""
    if fit_mode == "blur-contain":
        return (
            f"{pace}split=2[bga][fga];"
            "[bga]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:1[bg];"
            "[fga]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30,{fades}"
        )
    return f"{pace}scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,{fades}"