"""Deterministic sanity checks on each stage's output.

In the two-gate flow nobody watches TTS, captions, B-roll or the render.  A
silent voice file or a B-roll stage that failed every scene would previously
sail through to the final gate, wasting a render and the reviewer's time.
These checks stand in for the eyes that are no longer there.

Every check is plain Python over files already on disk: no Claude call, no
network, no cost.  That is a deliberate constraint -- the repo keeps
judgement deterministic in Python (see CLAUDE.md) and the planner's Claude
budget is already a tracked risk.  The trade-off is honest: these catch
"broken", not "ugly".  Aesthetic problems remain a job for the human at the
final gate.

Each check returns (ok, reason).  `reason` is Korean because it goes straight
into the operator's chat message when a job stops.
"""

from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path

from korean_grammar import verify_numbers_preserved
from script_runtime import load_guard_settings


def media_duration_seconds(path):
    """Length of an audio/video file in seconds, or None if undeterminable.

    WAV is read with the stdlib so the common case needs no subprocess; other
    containers fall back to ffprobe.  Returning None on failure is deliberate:
    a missing probe must not fail a job, it just means that particular
    assertion cannot be made.
    """
    path = Path(path)
    if not path.exists():
        return None
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as handle:
                rate = handle.getframerate()
                if rate:
                    return handle.getnframes() / float(rate)
        except (wave.Error, OSError):
            pass  # Not a plain PCM WAV; fall through to ffprobe.
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            text=True, capture_output=True, check=True,
        )
        return float(result.stdout.strip())
    except (subprocess.SubprocessError, ValueError, OSError):
        return None


def _nonempty_file(path):
    path = Path(path)
    return path.exists() and path.stat().st_size > 0


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def check_tts(work_dir, settings=None):
    """voice.wav exists and its length is plausible for the target duration."""
    settings = settings or load_guard_settings()
    path = Path(work_dir) / "voice.wav"
    if not _nonempty_file(path):
        return False, "voice.wav이 없거나 비어 있습니다."
    duration = media_duration_seconds(path)
    if duration is None:
        return True, "voice.wav 길이를 확인할 수 없어 길이 검사를 건너뜁니다."
    target = settings.target_duration_sec
    low = target * settings.tts_min_ratio
    high = target * settings.tts_max_ratio
    if not low <= duration <= high:
        return False, (
            f"음성 길이 {duration:.1f}초가 목표 {target}초의 허용 범위"
            f"({low:.0f}~{high:.0f}초)를 벗어났습니다."
        )
    return True, f"음성 {duration:.1f}초"


def _srt_cue_text(srt_text):
    """Just the caption lines, without the cue numbers and timing lines."""
    lines = []
    for line in srt_text.splitlines():
        line = line.strip()
        if not line or "-->" in line or line.isdigit():
            continue
        lines.append(line)
    return " ".join(lines)


def check_caption(work_dir, settings=None):
    """subs.srt has cues and scene timings do not run past the audio."""
    settings = settings or load_guard_settings()
    work_dir = Path(work_dir)
    srt = work_dir / "subs.srt"
    if not _nonempty_file(srt):
        return False, "subs.srt가 없거나 비어 있습니다."
    try:
        text = srt.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"subs.srt를 읽지 못했습니다: {exc}"
    if "-->" not in text:
        return False, "subs.srt에 자막 큐가 없습니다."

    # 숫자가 자막에서 어긋나면 근거 수치가 틀린 영상이 나간다. 소수점이 공백이
    # 되거나 단위가 잘리는 종류의 사고를 렌더 전에 잡는다.
    source = work_dir / "caption_script.txt"
    if source.is_file():
        try:
            script_text = source.read_text(encoding="utf-8", errors="replace")
        except OSError:
            script_text = ""
        if script_text and not verify_numbers_preserved(script_text, _srt_cue_text(text)):
            return False, (
                "자막의 숫자 표기가 대본과 다릅니다. "
                "소수점이나 단위가 잘렸을 수 있습니다."
            )

    scenes = _read_json(work_dir / "scenes_timed.json")
    if not isinstance(scenes, list) or not scenes:
        return False, "scenes_timed.json이 없거나 비어 있습니다."

    voice_duration = media_duration_seconds(work_dir / "voice.wav")
    if voice_duration is None:
        return True, f"자막 {len(scenes)}씬 (음성 길이 미확인으로 정렬 검사 생략)"
    ends = [float(scene.get("end") or 0) for scene in scenes if isinstance(scene, dict)]
    last_end = max(ends) if ends else 0.0
    overrun = last_end - voice_duration
    if overrun > settings.caption_max_overrun_sec:
        return False, (
            f"자막 마지막 시각 {last_end:.1f}초가 음성 {voice_duration:.1f}초를 "
            f"{overrun:.1f}초 초과했습니다. 정렬이 어긋났을 수 있습니다."
        )
    return True, f"자막 {len(scenes)}씬 / 마지막 {last_end:.1f}초"


def check_broll(work_dir, settings=None):
    """Not too many scenes ended up without a clip."""
    settings = settings or load_guard_settings()
    statuses = _read_json(Path(work_dir) / "broll_status.json")
    if not isinstance(statuses, list) or not statuses:
        return False, "broll_status.json이 없거나 비어 있습니다."
    total = len(statuses)
    failed = sum(1 for item in statuses if isinstance(item, dict) and item.get("status") == "failed")
    ratio = failed / total
    if ratio > settings.broll_max_failed_ratio:
        return False, (
            f"B-roll {total}씬 중 {failed}씬 실패({ratio:.0%})로 허용치"
            f"({settings.broll_max_failed_ratio:.0%})를 넘었습니다."
        )
    return True, f"B-roll {total}씬 중 {failed}씬 실패"


def _rendered_output_path(work_dir):
    """Where 2_render.sh says it wrote the video (it records output_path.txt)."""
    marker = Path(work_dir) / "output_path.txt"
    try:
        recorded = marker.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    return Path(recorded) if recorded else None


def check_render(work_dir, settings=None):
    """The rendered file exists and roughly matches the narration length."""
    settings = settings or load_guard_settings()
    output = _rendered_output_path(work_dir)
    if output is None:
        return False, "output_path.txt가 없어 렌더 결과 위치를 알 수 없습니다."
    if not _nonempty_file(output):
        return False, f"렌더 결과가 없거나 비어 있습니다: {output}"

    voice_duration = media_duration_seconds(Path(work_dir) / "voice.wav")
    video_duration = media_duration_seconds(output)
    if voice_duration is None or video_duration is None:
        return True, "렌더 완료 (길이 비교 생략)"
    gap = abs(video_duration - voice_duration)
    if gap > settings.render_duration_tolerance_sec:
        return False, (
            f"영상 {video_duration:.1f}초와 음성 {voice_duration:.1f}초 차이가 "
            f"{gap:.1f}초로 허용치({settings.render_duration_tolerance_sec:.0f}초)를 넘었습니다."
        )
    return True, f"렌더 {video_duration:.1f}초"


# Stage name -> check, so pipeline_flow can look one up by name without
# importing each function individually.
CHECKS = {
    "tts": check_tts,
    "caption": check_caption,
    "broll": check_broll,
    "render": check_render,
}


def check(stage, work_dir, settings=None):
    """Run the check for `stage`; stages without one always pass."""
    checker = CHECKS.get(stage)
    if checker is None:
        return True, ""
    return checker(work_dir, settings)
