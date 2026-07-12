"""Aspect-aware Pexels B-roll selection and normalization policy."""
import math
import os
import random

TARGET_ASPECT = float(os.environ.get("BROLL_CONTENT_ASPECT", str(1080 / 1300)))
PORTRAIT_TARGET_RATIO = float(os.environ.get("BROLL_PORTRAIT_TARGET_RATIO", "0.60"))
MAX_ORIENTATION_STREAK = int(os.environ.get("BROLL_MAX_ORIENTATION_STREAK", "2"))


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
        return quality - pixel_distance

    return max(files, key=file_score)


def _portrait_ratio(history):
    directed = [item for item in history if item in ("portrait", "landscape")]
    if not directed:
        return 0.0
    return directed.count("portrait") / len(directed)


def score_candidate(video, file_info, min_duration, used_video_ids, orientation_history, jitter=0.0):
    width, height = int(file_info["width"]), int(file_info["height"])
    kind = orientation(width, height)
    duration = float(video.get("duration") or 0)
    score = crop_retention(width, height) * 45
    score += min(min(width, height) / 720.0, 1.0) * 12
    score += 90 if video.get("id") not in used_video_ids else -35
    score += 70 if duration >= float(min_duration) else -45

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


def select_video(videos, min_duration, used_video_ids, orientation_history, rng=None):
    rng = rng or random
    ranked = []
    for video in videos:
        file_info = choose_video_file(video)
        if not file_info:
            continue
        ranked.append((
            score_candidate(video, file_info, min_duration, used_video_ids, orientation_history, rng.random() * 1.5),
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


def normalization_filter(fit_mode, duration, fade_duration=0.3):
    fade_out_start = max(float(duration) - fade_duration, 0)
    fades = f"fade=t=in:st=0:d={fade_duration},fade=t=out:st={fade_out_start}:d={fade_duration}"
    if fit_mode == "blur-contain":
        return (
            "split=2[bga][fga];"
            "[bga]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:1[bg];"
            "[fga]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,fps=30,{fades}"
        )
    return f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30,{fades}"