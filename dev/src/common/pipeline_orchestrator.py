"""Render/stage-advance orchestration for slack_bot.py and the CLI driver.

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

import contextlib
import threading
from pathlib import Path

import job_state
import pipeline_flow
from script_runtime import speech_pace_profile
# Single definition, shared by both bots and by the stage checks.
from stage_guard import media_duration_seconds  # noqa: F401  (re-exported)

# pipeline_flow gate -> the bot-facing stage token whose buttons handle it.
# script_review deliberately reuses the existing script-approval stage so all
# of its edit affordances (본문 수정 / 타이틀 수정) keep working unchanged.
GATE_STAGES = {
    "script_review": "await_script_approval",
    "thumbnail_intake": "await_thumbnail_intake",
    "x_photo_intake": "await_x_photo_intake",
    "final_confirm": "await_final_confirm",
}

# Bot stage token -> the pipeline_flow stage that has finished by then.
# "await_X_approval" means X produced its output and is waiting for a human,
# so the flow's completed stage is X.  Lets an unattended run pick up from
# wherever a gated run stopped.
BOT_STAGE_TO_FLOW = {
    "await_script_approval": "script",
    "await_tts_approval": "tts",
    "await_caption_approval": "caption",
    "await_broll_approval": "broll",
    "await_render_config": "broll",
    "await_thumbnail_intake": "broll",
    "await_x_photo_intake": "x_thread",
    "await_render_approval": "render",
    "await_upload_meta_approval": "render",
    "await_final_confirm": "render",
}

STAGE_PROGRESS_LABELS = {
    "script": "스크립트 생성",
    "scene_visuals": "씬 시각 계획",
    "x_thread": "X 스레드 초안 생성",
    "tts": "TTS 음성 생성",
    "caption": "자막 생성",
    "broll": "B-roll 수집",
    "render": "렌더링",
    "upload": "YouTube 비공개 업로드",
    "x_post": "X 게시",
}


def display_config_value(value):
    return str(value) if value not in (None, "") else "config"


def preview_file(path, limit):
    """First `limit` characters of a text artifact, for a chat preview."""
    path = Path(path)
    if not path.exists():
        return "파일이 없습니다."
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > limit:
        return text[:limit] + "\n...(생략)"
    return text


def render_progress_ratio(progress_path, duration):
    """How far ffmpeg has got, from the -progress file it writes, as 0..1."""
    progress_path = Path(progress_path)
    if not progress_path.exists() or not duration:
        return None
    try:
        lines = progress_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    seconds = None
    for line in lines:
        if line.startswith("out_time_ms=") or line.startswith("out_time_us="):
            with contextlib.suppress(ValueError):
                seconds = int(line.split("=", 1)[1]) / 1_000_000
        elif line.startswith("out_time="):
            value = line.split("=", 1)[1]
            try:
                hours, minutes, rest = value.split(":")
                seconds = int(hours) * 3600 + int(minutes) * 60 + float(rest)
            except ValueError:
                pass
    if seconds is None:
        return None
    return max(0.0, min(seconds / duration, 1.0))


# job key -> environment variable, for settings that pass through as text.
# A table rather than forty near-identical `if` statements: adding a knob is
# one line here, and it is obvious at a glance which keys are forwarded.
_ENV_KEYS = {
    "caption_font_size": "CAPTION_FONT_SIZE",
    "caption_margin_v": "CAPTION_MARGIN_V",
    "caption_margin_h": "CAPTION_MARGIN_H",
    "caption_style": "CAPTION_STYLE",
    "caption_offset_x": "CAPTION_OFFSET_X",
    "caption_offset_y": "CAPTION_OFFSET_Y",
    "frame_mode": "FRAME_MODE",
    "broll_fit_mode": "BROLL_FIT_MODE",
    "frame_top_preset": "FRAME_TOP_PRESET",
    "frame_bottom_preset": "FRAME_BOTTOM_PRESET",
    "frame_top_pct": "FRAME_TOP_PCT",
    "frame_bottom_pct": "FRAME_BOTTOM_PCT",
    "frame_bottom_channel_name": "FRAME_BOTTOM_CHANNEL_NAME",
    "channel_style": "CHANNEL_STYLE",
    "frame_header_text": "FRAME_HEADER_TEXT",
    "tts_voice": "TTS_VOICE",
    "youtube_feedback_strictness": "YOUTUBE_FEEDBACK_STRICTNESS",
    "target_duration_sec": "TARGET_DURATION_SEC",
    "claude_script_model": "CLAUDE_SCRIPT_MODEL",
    "claude_research_model": "CLAUDE_RESEARCH_MODEL",
    "claude_strategy_model": "CLAUDE_STRATEGY_MODEL",
    "claude_query_model": "CLAUDE_QUERY_MODEL",
}

# Same idea, for the flags the shell scripts expect as "true"/"false".
_ENV_BOOL_KEYS = {
    "web_research": "ENABLE_WEB_RESEARCH",
    "case_research": "ENABLE_CASE_RESEARCH",
    "youtube_feedback_auto_sync": "YOUTUBE_FEEDBACK_AUTO_SYNC",
}


def build_extra_env(job):
    env = {}
    for job_key, env_key in _ENV_KEYS.items():
        if job_key in job:
            env[env_key] = str(job[job_key])
    for job_key, env_key in _ENV_BOOL_KEYS.items():
        if job_key in job:
            env[env_key] = "true" if job.get(job_key) else "false"
    # Pace expands into two variables via a profile, so it stays explicit.
    if "speech_pace" in job:
        pace, profile = speech_pace_profile(job["speech_pace"])
        env["SPEECH_PACE"] = pace
        env["ATEMPO"] = str(profile["atempo"])
    if job.get("claude_budget_override"):
        env["CLAUDE_BUDGET_OVERRIDE"] = "true"
    return env


def _render_args(ctx, job):
    args = [str(ctx.BASE_DIR / "sh" / "youtube" / "2_render.sh")]
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
    if job.get("mode"):
        # Redone outside pipeline_flow (job history rework): keep job_state.json's
        # position honest so the next "다음 단계 ▶" resumes after render instead
        # of trusting a stale, earlier-recorded position (e.g. already "x_post").
        pipeline_flow.mark_stage_done(ctx.work_dir(job_id), "render")
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


# ── 2게이트(review) 모드 ────────────────────────────────────────────────
# The stage order, the between-stage checks and the retry policy all live in
# pipeline_flow.  This layer only translates: job dict -> flow inputs, and
# flow results -> chat messages.  Nothing about the pipeline is described
# twice, and the same core drives run_pipeline.py for unattended runs.

def _job_settings(job):
    return {
        "topic": job.get("topic"),
        "topic_json": job.get("topic_json"),
        "allow_no_pubmed": job.get("allow_no_pubmed"),
    }


def _review_runner(ctx, chat_id, job):
    """Adapt the bot's run_command into the callable pipeline_flow expects."""
    job_id = job["job_id"]
    topic = job.get("topic")
    settings = _job_settings(job)
    extra_env = build_extra_env(job)

    def run(stage_name, argv):
        if stage_name == "render":
            # Go through the render helper so the chat still gets 25/50/75%
            # ticks; a 1080p render is long enough that silence reads as a hang.
            # Prefer the bot's own wrapper, matching how every other primitive
            # in this module is reached -- by attribute on ctx.
            render = getattr(ctx, "_run_render_silent", None)
            if render is not None:
                render(chat_id, job, extra_env)
            else:
                run_render_silent(ctx, chat_id, job, extra_env)
            return
        full_argv = list(argv) + pipeline_flow.stage_extra_args(stage_name, settings)
        ctx.run_command(full_argv, job_id, topic, extra_env=extra_env)

    return run


def _passthrough_exceptions(ctx):
    """Exceptions that end the run outright instead of failing one stage.

    A user cancellation or an exhausted Claude budget must not be retried --
    retrying is exactly what the operator asked to stop.
    """
    return tuple(getattr(ctx, "PASSTHROUGH_EXCEPTIONS", ()))


def _review_progress(ctx, chat_id, job=None, remaining=None):
    """Relay stage transitions to the chat so an unattended stretch is visible.

    When `remaining` is given, steps are numbered i/N the way the old
    hand-written auto flows did, and the current step is mirrored into the job
    so a status query mid-run says something useful.
    """
    total = len(remaining) if remaining else 0
    order = list(remaining or [])

    def on_event(kind, payload):
        name = payload.get("stage", "")
        label = STAGE_PROGRESS_LABELS.get(name, name)
        if kind == "stage_start":
            suffix = " (재시도)" if payload.get("retry") else ""
            if total and name in order:
                step = f"{order.index(name) + 1}/{total} {label}"
                if job is not None:
                    job["auto_progress"] = step
                ctx.send_message(chat_id, f"{step} 중...{suffix}")
            else:
                ctx.send_message(chat_id, f"{label} 진행 중...{suffix}")
        elif kind == "guard_failed":
            ctx.send_message(chat_id, f"{label} 점검 실패: {payload.get('reason', '')}")

    return on_event


def stages_remaining_after(flow_stage):
    """Flow stage names still to run once `flow_stage` has completed."""
    if flow_stage is None:
        return list(pipeline_flow.STAGE_NAMES)
    index = pipeline_flow.STAGE_NAMES.index(flow_stage)
    return list(pipeline_flow.STAGE_NAMES[index + 1:])


def run_to_completion(ctx, chat_id, job, *, final_message=None, running_stage=None):
    """Run everything left, unattended, from wherever the job currently is.

    This is what /run_auto and "여기서부터 끝까지" both do.  Both bots used to
    spell the stage sequence out by hand -- four copies that had to be kept in
    step with the real pipeline.  The order now comes from pipeline_flow, and
    the guards run here too, so an unattended finish gets the same checks the
    two-gate flow gets.
    """
    job_id = job.get("job_id")
    if not job_id:
        ctx.send_message(chat_id, "진행 중인 작업이 없습니다.")
        return None

    start_stage = job.get("stage")
    flow_stage = BOT_STAGE_TO_FLOW.get(start_stage)
    if start_stage is not None and start_stage not in BOT_STAGE_TO_FLOW:
        ctx.send_message(chat_id, f"현재 단계에서는 끝까지 자동 처리를 시작할 수 없습니다: {start_stage}")
        return None

    work_dir = ctx.work_dir(job_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    job["mode"] = job_state.MODE_AUTO
    job["approval_required"] = False
    job.pop("last_error", None)
    if running_stage:
        job["stage"] = running_stage
    job_state.update(
        work_dir, mode=job_state.MODE_AUTO, stage=flow_stage,
        awaiting=None, cleared_gate=None, settings=_job_settings(job),
    )

    remaining = stages_remaining_after(flow_stage)
    try:
        result = pipeline_flow.advance(
            work_dir, ctx.BASE_DIR, _review_runner(ctx, chat_id, job),
            mode=job_state.MODE_AUTO,
            on_event=_review_progress(ctx, chat_id, job, remaining),
            passthrough=_passthrough_exceptions(ctx),
        )
    except BaseException:
        # Cancellation or a budget stop: put the job back where it started so
        # the operator resumes from a known point, then let the bot's own
        # handler present it.
        job["stage"] = start_stage
        job.pop("auto_progress", None)
        raise

    job.pop("auto_progress", None)
    if result.status == pipeline_flow.STATUS_DONE:
        job["stage"] = "done"
        default_text = "업로드 완료. YouTube Studio에서 비공개 영상을 확인하세요."
        ctx.send_message(chat_id, final_message(job, default_text) if final_message else default_text)
        return result

    if result.status == pipeline_flow.STATUS_GATE:
        # Auto otherwise never parks, but thumbnail_intake is honoured even
        # here (see pipeline_flow.MODE_GATES) -- this is not a failure, just
        # the one thing auto still waits on a human for.
        job["stage"] = GATE_STAGES.get(result.gate, result.gate)
        ctx.send_gate(chat_id, job, result.gate)
        return result

    # Unattended runs have no other gate to fall back to, so a failure is an
    # error. Raising keeps the bots' existing recovery prompts working
    # unchanged.
    job["stage"] = start_stage
    job["last_error"] = result.reason
    raise RuntimeError(f"{result.stage} 단계에서 중단했습니다: {result.reason}")


def run_review_pipeline(ctx, chat_id, job, mode=job_state.MODE_REVIEW):
    """Run the job forward until it needs a human, then hand off to the bot.

    Returns the pipeline_flow.Result so callers can branch if they need to.
    """
    job_id = job["job_id"]
    work_dir = ctx.work_dir(job_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    job["mode"] = mode
    job_state.update(work_dir, mode=mode, settings=_job_settings(job))

    result = pipeline_flow.advance(
        work_dir, ctx.BASE_DIR, _review_runner(ctx, chat_id, job),
        mode=mode, on_event=_review_progress(ctx, chat_id),
        passthrough=_passthrough_exceptions(ctx),
    )

    if result.status == pipeline_flow.STATUS_GATE:
        job["stage"] = GATE_STAGES.get(result.gate, result.gate)
        ctx.send_gate(chat_id, job, result.gate)
    elif result.status == pipeline_flow.STATUS_DONE:
        job["stage"] = "done"
        ctx.send_message(chat_id, "완료! YouTube Studio에서 비공개 영상을 확인하세요.")
    else:
        job["stage"] = "failed"
        ctx.send_message(
            chat_id,
            f"{result.stage} 단계에서 중단했습니다.\n사유: {result.reason}\n\n"
            f"수정 후 /resume 으로 이어서 진행하거나 /rewind {result.stage} 로 "
            "해당 단계를 다시 실행하세요.",
        )
    return result


def approve_review_gate(ctx, chat_id, job):
    """Clear the current gate and keep going (the human pressed 승인)."""
    work_dir = ctx.work_dir(job["job_id"])
    pipeline_flow.approve(work_dir)
    return run_review_pipeline(ctx, chat_id, job, mode=job.get("mode") or job_state.MODE_REVIEW)


def switch_to_review(ctx, chat_id, job, from_stage="script"):
    """Move a job already past `from_stage` onto the two-gate flow.

    Lets the reviewer decide at the script gate -- once they have actually
    read it -- whether the rest runs unattended or comes back for a final
    look, instead of committing to a mode before the script exists.
    """
    work_dir = ctx.work_dir(job["job_id"])
    job_state.update(
        work_dir,
        mode=job_state.MODE_REVIEW,
        stage=from_stage,
        awaiting=None,
        cleared_gate="script_review",
        settings=_job_settings(job),
    )
    return run_review_pipeline(ctx, chat_id, job)


def run_next_stage(ctx, chat_id, job, *, final_message=None):
    job_id = job["job_id"]
    topic = job.get("topic")
    stage = job.get("stage")

    extra_env = build_extra_env(job)
    if stage == "await_script_approval":
        # This legacy chain bypasses pipeline_flow entirely, so the stage it
        # inserted after `script` has to be repeated here -- without it the
        # step-by-step path reaches 3_broll.py with no visual_query.
        ctx.run_command(
            [str(ctx.BASE_DIR / "sh" / "common" / "scene_visuals.sh")], job_id, topic, extra_env=extra_env,
        )
        ctx.send_message(chat_id, "TTS 생성 시작")
        ctx.run_command([str(ctx.BASE_DIR / "sh" / "youtube" / "1_tts.sh")], job_id, topic, extra_env=extra_env)
        job["stage"] = "await_tts_approval"
        ctx.send_tts(chat_id, job_id)
    elif stage == "await_tts_approval":
        ctx.send_message(chat_id, "자막 생성 시작")
        ctx.run_command([str(ctx.BASE_DIR / "sh" / "youtube" / "1_caption.sh")], job_id, topic, extra_env=extra_env)
        job["stage"] = "await_caption_approval"
        ctx.send_caption(chat_id, job_id)
    elif stage == "await_caption_approval":
        ctx.send_message(chat_id, "B-roll 생성 시작")
        ctx.run_command([str(ctx.BASE_DIR / "sh" / "youtube" / "1_broll.sh")], job_id, topic, extra_env=extra_env)
        job["stage"] = "await_broll_approval"
        ctx.send_broll(chat_id, job_id)
    elif stage == "await_broll_approval":
        job["stage"] = "await_render_config"
        ctx.send_render_ready(chat_id, job)
    elif stage == "await_render_config":
        job["stage"] = "await_thumbnail_intake"
        ctx.send_thumbnail_prompt(chat_id, job_id)
    elif stage == "await_thumbnail_intake":
        ctx.run_render(chat_id, job)
    elif stage == "await_render_approval":
        job["stage"] = "await_upload_meta_approval"
        ctx.send_upload_meta(chat_id, job_id)
    elif stage == "await_upload_meta_approval":
        ctx.send_message(chat_id, "YouTube 비공개 업로드 시작")
        ctx.run_command([str(ctx.BASE_DIR / "sh" / "youtube" / "3_upload.sh")], job_id, topic, extra_env=extra_env)
        job["stage"] = "done"
        default_text = "업로드 완료. YouTube Studio에서 비공개 영상을 확인하세요."
        ctx.send_message(chat_id, final_message(job, default_text) if final_message else default_text)
    else:
        ctx.send_message(chat_id, f"승인할 단계가 없습니다. 현재 단계: {stage}")
