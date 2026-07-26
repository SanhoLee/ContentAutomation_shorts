"""Shared render/stage-advance orchestration for telegram_bot.py and slack_bot.py.

Both bots wrap the same pipeline stages (TTS -> caption -> B-roll -> render ->
upload) with identical or near-identical orchestration code; only the
transport primitives (how a message/file is actually sent) differ. Functions
here take `ctx` -- the calling bot's own module object -- and call back into
it by attribute for those primitives (send_message, run_command,
start_render_progress, send_tts/send_caption/send_broll/send_render_ready/
send_upload_meta, BASE_DIR). Each bot module already defines all of these
under the same names, so passing `sys.modules[__name__]` as ctx just reuses
them without needing a formal adapter class.
"""

from __future__ import annotations

import threading

from script_runtime import speech_pace_profile


def display_config_value(value):
    return str(value) if value not in (None, "") else "config"


def build_extra_env(job):
    env = {}
    if "caption_font_size" in job:
        env["CAPTION_FONT_SIZE"] = str(job["caption_font_size"])
    if "caption_margin_v" in job:
        env["CAPTION_MARGIN_V"] = str(job["caption_margin_v"])
    if "caption_margin_h" in job:
        env["CAPTION_MARGIN_H"] = str(job["caption_margin_h"])
    if "caption_style" in job:
        env["CAPTION_STYLE"] = str(job["caption_style"])
    if "caption_offset_x" in job:
        env["CAPTION_OFFSET_X"] = str(job["caption_offset_x"])
    if "caption_offset_y" in job:
        env["CAPTION_OFFSET_Y"] = str(job["caption_offset_y"])
    if "frame_mode" in job:
        env["FRAME_MODE"] = str(job["frame_mode"])
    if "broll_fit_mode" in job:
        env["BROLL_FIT_MODE"] = str(job["broll_fit_mode"])
    if "frame_top_preset" in job:
        env["FRAME_TOP_PRESET"] = str(job["frame_top_preset"])
    if "frame_bottom_preset" in job:
        env["FRAME_BOTTOM_PRESET"] = str(job["frame_bottom_preset"])
    if "frame_top_pct" in job:
        env["FRAME_TOP_PCT"] = str(job["frame_top_pct"])
    if "frame_bottom_pct" in job:
        env["FRAME_BOTTOM_PCT"] = str(job["frame_bottom_pct"])
    if "frame_bottom_channel_name" in job:
        env["FRAME_BOTTOM_CHANNEL_NAME"] = str(job["frame_bottom_channel_name"])
    if "frame_header_text" in job:
        env["FRAME_HEADER_TEXT"] = str(job["frame_header_text"])
    if "tts_voice" in job:
        env["TTS_VOICE"] = str(job["tts_voice"])
    if "web_research" in job:
        env["ENABLE_WEB_RESEARCH"] = "true" if job.get("web_research") else "false"
    if "case_research" in job:
        env["ENABLE_CASE_RESEARCH"] = "true" if job.get("case_research") else "false"
    if "youtube_feedback_strictness" in job:
        env["YOUTUBE_FEEDBACK_STRICTNESS"] = str(job["youtube_feedback_strictness"])
    if "youtube_feedback_auto_sync" in job:
        env["YOUTUBE_FEEDBACK_AUTO_SYNC"] = "true" if job.get("youtube_feedback_auto_sync") else "false"
    if "speech_pace" in job:
        pace, profile = speech_pace_profile(job["speech_pace"])
        env["SPEECH_PACE"] = pace
        env["ATEMPO"] = str(profile["atempo"])
    if "target_duration_sec" in job:
        env["TARGET_DURATION_SEC"] = str(job["target_duration_sec"])
    if "claude_script_model" in job:
        env["CLAUDE_SCRIPT_MODEL"] = str(job["claude_script_model"])
    if "claude_research_model" in job:
        env["CLAUDE_RESEARCH_MODEL"] = str(job["claude_research_model"])
    if "claude_strategy_model" in job:
        env["CLAUDE_STRATEGY_MODEL"] = str(job["claude_strategy_model"])
    if "claude_query_model" in job:
        env["CLAUDE_QUERY_MODEL"] = str(job["claude_query_model"])
    if job.get("claude_budget_override"):
        env["CLAUDE_BUDGET_OVERRIDE"] = "true"
    return env


def _render_args(ctx, job):
    args = [str(ctx.BASE_DIR / "sh" / "2_render.sh")]
    font_size = job.get("caption_font_size")
    margin_v = job.get("caption_margin_v")
    margin_h = job.get("caption_margin_h")
    caption_style = job.get("caption_style")
    offset_x = job.get("caption_offset_x")
    offset_y = job.get("caption_offset_y")
    frame_mode = job.get("frame_mode")
    broll_fit = job.get("broll_fit_mode")
    frame_top_preset = job.get("frame_top_preset")
    frame_bottom_preset = job.get("frame_bottom_preset")
    frame_top_pct = job.get("frame_top_pct")
    frame_bottom_pct = job.get("frame_bottom_pct")
    frame_top_title = job.get("frame_top_title", job.get("frame_header_text", ""))
    frame_top_subtitle = job.get("frame_top_subtitle", "")
    frame_bottom_channel = job.get("frame_bottom_channel_name", "")

    def add_arg(flag, value):
        if value not in (None, ""):
            args.extend([flag, str(value)])

    add_arg("--font-size", font_size)
    add_arg("--margin-v", margin_v)
    add_arg("--margin-h", margin_h)
    add_arg("--style", caption_style)
    add_arg("--offset-x", offset_x)
    add_arg("--offset-y", offset_y)
    add_arg("--frame-mode", frame_mode)
    add_arg("--broll-fit", broll_fit)
    add_arg("--frame-top-preset", frame_top_preset)
    add_arg("--frame-bottom-preset", frame_bottom_preset)
    add_arg("--frame-top-pct", frame_top_pct)
    add_arg("--frame-bottom-pct", frame_bottom_pct)
    add_arg("--top-title", frame_top_title)
    add_arg("--top-subtitle", frame_top_subtitle)
    add_arg("--bottom-channel-name", frame_bottom_channel)
    return args, {
        "font_size": font_size, "margin_v": margin_v, "margin_h": margin_h,
        "caption_style": caption_style, "offset_x": offset_x, "offset_y": offset_y,
        "frame_mode": frame_mode, "broll_fit": broll_fit,
    }


def run_render(ctx, chat_id, job):
    job_id = job["job_id"]
    args, summary = _render_args(ctx, job)
    extra_env = build_extra_env(job)
    ctx.send_message(
        chat_id,
        "렌더링 시작: font=" + display_config_value(summary["font_size"]) +
        ", margin_v=" + display_config_value(summary["margin_v"]) + ", margin_h=" + display_config_value(summary["margin_h"]) +
        ", style=" + display_config_value(summary["caption_style"]) +
        ", offset_x=" + display_config_value(summary["offset_x"]) + ", offset_y=" + display_config_value(summary["offset_y"]) +
        ", frame=" + display_config_value(summary["frame_mode"]) + ", broll_fit=" + display_config_value(summary["broll_fit"]),
    )
    stop_progress = threading.Event()
    progress_thread = ctx.start_render_progress(chat_id, job_id, stop_progress)
    try:
        ctx.run_command(args, job_id, job.get("topic"), extra_env=extra_env)
    finally:
        stop_progress.set()
        if progress_thread:
            progress_thread.join(timeout=1)
    ctx.send_message(chat_id, "렌더링 진행률: 완료")
    job["stage"] = "await_render_approval"
    ctx.send_rendered_video(chat_id, job_id)


def run_render_silent(ctx, chat_id, job, extra_env=None):
    job_id = job["job_id"]
    args, _ = _render_args(ctx, job)
    env = build_extra_env(job)
    env.update(extra_env or {})
    stop_progress = threading.Event()
    progress_thread = ctx.start_render_progress(chat_id, job_id, stop_progress)
    try:
        ctx.run_command(args, job_id, job.get("topic"), extra_env=env)
    finally:
        stop_progress.set()
        if progress_thread:
            progress_thread.join(timeout=1)


def run_next_stage(ctx, chat_id, job, *, final_message=None):
    job_id = job["job_id"]
    topic = job.get("topic")
    stage = job.get("stage")

    extra_env = build_extra_env(job)
    if stage == "await_script_approval":
        ctx.send_message(chat_id, "TTS 생성 시작")
        ctx.run_command([str(ctx.BASE_DIR / "sh" / "1_tts.sh")], job_id, topic, extra_env=extra_env)
        job["stage"] = "await_tts_approval"
        ctx.send_tts(chat_id, job_id)
    elif stage == "await_tts_approval":
        ctx.send_message(chat_id, "자막 생성 시작")
        ctx.run_command([str(ctx.BASE_DIR / "sh" / "1_caption.sh")], job_id, topic, extra_env=extra_env)
        job["stage"] = "await_caption_approval"
        ctx.send_caption(chat_id, job_id)
    elif stage == "await_caption_approval":
        ctx.send_message(chat_id, "B-roll 생성 시작")
        ctx.run_command([str(ctx.BASE_DIR / "sh" / "1_broll.sh")], job_id, topic, extra_env=extra_env)
        job["stage"] = "await_broll_approval"
        ctx.send_broll(chat_id, job_id)
    elif stage == "await_broll_approval":
        job["stage"] = "await_render_config"
        ctx.send_render_ready(chat_id, job)
    elif stage == "await_render_config":
        ctx.run_render(chat_id, job)
    elif stage == "await_render_approval":
        job["stage"] = "await_upload_meta_approval"
        ctx.send_upload_meta(chat_id, job_id)
    elif stage == "await_upload_meta_approval":
        ctx.send_message(chat_id, "YouTube 비공개 업로드 시작")
        ctx.run_command([str(ctx.BASE_DIR / "sh" / "3_upload.sh")], job_id, topic, extra_env=extra_env)
        job["stage"] = "done"
        default_text = "업로드 완료. YouTube Studio에서 비공개 영상을 확인하세요."
        ctx.send_message(chat_id, final_message(job, default_text) if final_message else default_text)
    else:
        ctx.send_message(chat_id, f"승인할 단계가 없습니다. 현재 단계: {stage}")
