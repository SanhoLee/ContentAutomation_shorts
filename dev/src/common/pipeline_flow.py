"""The pipeline as data: one stage graph, three gate policies.

Until now the stage order existed in three places that could drift -- the
if/elif chain in pipeline_orchestrator.run_next_stage, the hardcoded five-step
sequence in each bot's run_remaining_to_upload, and the shell commands listed
in run_goal.sh.  Adding a run mode meant editing all of them.

Here the order lives in STAGES once, and a run mode is nothing but the set of
gates it honours:

    auto       -- honour none; run start to finish unattended
    review     -- honour script_review and final_confirm (the two-gate flow)
    full_gate  -- honour every gate (the legacy per-stage approval flow)

`advance()` runs stages until it reaches a gate the mode honours, finishes, or
gives up.  It never blocks waiting for a human: it parks the job in
job_state.json and returns, so the caller can be a bot callback, a cron-driven
CLI, or a test -- all resuming the same job from the same file.

The module runs no commands itself.  `runner` is injected, which keeps the
graph free of subprocess/transport concerns and makes every path testable with
a fake runner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import job_state
import stage_guard


@dataclass(frozen=True)
class Stage:
    name: str
    # argv template; entries containing "/" are resolved against base_dir.
    command: tuple[str, ...]
    # Gates a human may be parked at after this stage, in order.  Which ones
    # actually stop the job is decided by the mode, not here.
    gates: tuple[str, ...] = ()
    # Key into stage_guard.CHECKS; None means nothing to verify.
    guard: str | None = None
    # Cheaper targeted retry used instead of re-running `command` on failure.
    retry_command: tuple[str, ...] | None = None


STAGES = (
    Stage(
        name="script",
        command=("sh/common/0_script.sh",),
        # x_thread_confirm rides along right after script_review so the
        # operator decides whether to spend an X-thread API call while the
        # script is still fresh in front of them -- before scene_visuals or
        # x_thread have run at all.
        gates=("script_review", "x_thread_confirm"),
    ),
    Stage(
        # Placed after script's gate on purpose: advance() parks *after* a
        # stage completes, so by the time this runs script.txt is the text
        # the operator approved. Planning visuals inside Stage 2 instead --
        # before anyone could edit -- is what left scenes.json describing a
        # script that no longer existed. Ahead of x_thread because that
        # stage reads the content_package.json this one refreshes.
        name="scene_visuals",
        command=("sh/common/scene_visuals.sh",),
    ),
    Stage(
        # No gate of its own: the draft it writes is picked up and shown
        # alongside the video at render's final_confirm gate, so review of
        # both happens in that one decision instead of a third approval.
        # The wrapper script always exits 0 -- a failure here (Claude
        # outage, budget guard) must not block the YouTube pipeline, since
        # this is a downstream-only artifact (see x_thread_adapter.py).
        name="x_thread",
        command=("sh/common/x_thread.sh",),
        # thumbnail_intake rides along here too (not on broll where it used
        # to live) so both image asks land back-to-back for the operator,
        # before tts/caption/broll do any real work -- see slack_bot.py's
        # send_thumbnail_prompt for why nothing downstream needs it earlier.
        gates=("x_photo_intake", "thumbnail_intake"),
    ),
    Stage(
        name="tts",
        command=("sh/youtube/1_tts.sh",),
        gates=("tts_review",),
        guard="tts",
    ),
    Stage(
        name="caption",
        command=("sh/youtube/1_caption.sh",),
        gates=("caption_review",),
        guard="caption",
    ),
    Stage(
        name="broll",
        command=("sh/youtube/1_broll.sh",),
        gates=("broll_review", "render_config"),
        guard="broll",
        # Re-searching only the failed scenes costs a fraction of the API
        # calls a full re-run would, which matters for an unattended retry.
        retry_command=("sh/youtube/1b_retry_broll.sh",),
    ),
    Stage(
        name="render",
        command=("sh/youtube/2_render.sh",),
        gates=("final_confirm",),
        guard="render",
    ),
    Stage(
        name="upload",
        command=("sh/youtube/3_upload.sh",),
    ),
    Stage(
        # Runs immediately after the YouTube upload, no gate: the operator
        # already approved this thread's content at final_confirm. Also
        # always exits 0 -- a posting failure (e.g. billing, X API outage)
        # must not mark the whole job failed when the video itself is
        # already safely uploaded; the standalone /x_post command remains
        # available to retry.
        name="x_post",
        command=("sh/common/x_post.sh",),
    ),
)

STAGE_NAMES = tuple(stage.name for stage in STAGES)
STAGES_BY_NAME = {stage.name: stage for stage in STAGES}

ALL_GATES = tuple(gate for stage in STAGES for gate in stage.gates)

MODE_GATES = {
    # Auto otherwise honours no gate, but the thumbnail hold is an explicit
    # exception the operator asked for: even a fully unattended run stops
    # here rather than shipping a video nobody had a chance to brand.
    job_state.MODE_AUTO: frozenset({"thumbnail_intake", "x_photo_intake"}),
    # x_thread_confirm depends on a human having actually read the script,
    # which auto mode never does -- script_review itself isn't forced there
    # either, so this stays paired with it instead of joining the auto set.
    job_state.MODE_REVIEW: frozenset({
        "script_review", "x_thread_confirm", "thumbnail_intake", "x_photo_intake", "final_confirm",
    }),
    job_state.MODE_FULL_GATE: frozenset(ALL_GATES),
}

# A stage gets one retry, then stops for a human.  Deliberately not a retry
# loop: repeated Claude/Pexels calls cost money and a stuck job that keeps
# paying is worse than one that waits (see KNOWN_ISSUES.md).
MAX_ATTEMPTS_PER_STAGE = 2

# advance() outcomes.
STATUS_GATE = "gate"      # parked, waiting for a human
STATUS_DONE = "done"      # every stage finished
STATUS_FAILED = "failed"  # a guard failed twice, or a command errored


@dataclass
class Result:
    status: str
    stage: str | None = None
    gate: str | None = None
    reason: str = ""
    completed: list[str] = field(default_factory=list)

    @property
    def ok(self):
        return self.status != STATUS_FAILED


def gates_for_mode(mode):
    if mode not in MODE_GATES:
        raise ValueError(f"unknown mode: {mode}")
    return MODE_GATES[mode]


def resolve_command(base_dir, command):
    """Turn a command template into an argv list rooted at base_dir."""
    base_dir = Path(base_dir)
    return [str(base_dir / part) if "/" in part else part for part in command]


def stage_extra_args(stage_name, settings):
    """Per-stage argv the caller supplies, from the job's stored settings.

    Only the script stage needs any: which topic to write about, either as
    free text or as a planner-produced topic_plan.json.  Kept here so every
    driver (bot, CLI, run_goal.sh) passes stage arguments the same way.
    """
    if stage_name != "script":
        return []
    if settings.get("topic_json"):
        return ["--topic-json", str(settings["topic_json"])]
    if settings.get("topic"):
        prefix = ["--allow-no-pubmed"] if settings.get("allow_no_pubmed") else []
        return prefix + [str(settings["topic"])]
    return []


def next_stage_after(stage_name):
    """The stage following `stage_name`, or the first when it is None."""
    if stage_name is None:
        return STAGES[0]
    if stage_name not in STAGES_BY_NAME:
        raise ValueError(f"unknown stage: {stage_name}")
    index = STAGE_NAMES.index(stage_name)
    if index + 1 >= len(STAGES):
        return None
    return STAGES[index + 1]


def pending_gate(stage, mode, after_gate=None):
    """The next gate of `stage` this mode honours, or None to keep going.

    `after_gate` is the gate just approved, so a stage with several gates
    (full_gate mode's B-roll review then render config) resumes at the right
    one instead of re-showing the one the human just cleared.
    """
    honoured = gates_for_mode(mode)
    gates = list(stage.gates)
    start = gates.index(after_gate) + 1 if after_gate in gates else 0
    for gate in gates[start:]:
        if gate in honoured:
            return gate
    return None


def _emit(on_event, kind, **payload):
    if on_event is not None:
        on_event(kind, payload)


def _run_stage_once(stage, work_dir, base_dir, runner, use_retry=False, passthrough=()):
    """Execute a stage; returns (ok, reason).

    `passthrough` names exceptions that mean "this run is over" rather than
    "this stage failed" -- a user cancellation or a blown API budget.  The
    flow must not convert those into a retryable stage failure, but it also
    should not know their types, so the caller supplies them.
    """
    command = stage.retry_command if use_retry and stage.retry_command else stage.command
    argv = resolve_command(base_dir, command)
    try:
        runner(stage.name, argv)
    except passthrough:
        raise
    except Exception as exc:
        return False, f"{stage.name} 실행 실패: {exc}"
    return True, ""


def run_stage(stage, work_dir, base_dir, runner, guard_settings=None, on_event=None,
              passthrough=()):
    """Run one stage, verify it, and retry once if the check fails.

    Returns (ok, reason).  The retry uses `retry_command` when the stage has
    one, so B-roll re-searches only its failed scenes rather than everything.
    """
    for attempt in range(1, MAX_ATTEMPTS_PER_STAGE + 1):
        is_retry = attempt > 1
        job_state.record_attempt(work_dir, stage.name)
        _emit(on_event, "stage_start", stage=stage.name, attempt=attempt, retry=is_retry)

        ok, reason = _run_stage_once(stage, work_dir, base_dir, runner,
                                     use_retry=is_retry, passthrough=passthrough)
        if not ok:
            # A command that errored will not be fixed by running it again
            # with identical inputs; stop and let a human look.
            _emit(on_event, "stage_failed", stage=stage.name, reason=reason)
            return False, reason

        if stage.guard is None:
            _emit(on_event, "stage_done", stage=stage.name, reason="")
            return True, ""

        ok, reason = stage_guard.check(stage.guard, work_dir, guard_settings)
        if ok:
            _emit(on_event, "stage_done", stage=stage.name, reason=reason)
            return True, reason

        _emit(on_event, "guard_failed", stage=stage.name, attempt=attempt, reason=reason)
        if attempt >= MAX_ATTEMPTS_PER_STAGE:
            return False, reason

    return False, "재시도 한도를 초과했습니다."


def advance(work_dir, base_dir, runner, mode=None, guard_settings=None, on_event=None,
            passthrough=()):
    """Run the job forward until a gate, the end, or a failure.

    The job must not already be parked -- call job_state.resume() when the
    human approves, then call this again.  Every outcome is persisted before
    returning, so a killed process loses nothing but the stage in flight.
    """
    state = job_state.load(work_dir)
    mode = mode or state.get("mode") or job_state.MODE_REVIEW
    if mode != state.get("mode"):
        state = job_state.set_mode(work_dir, mode)

    if state.get("awaiting"):
        return Result(
            status=STATUS_GATE, stage=state.get("stage"), gate=state["awaiting"],
            reason="승인 대기 중입니다.",
        )

    completed = []
    # A stage may have more than one gate; remember which one was just
    # cleared so we resume after it instead of before it.
    cleared_gate = state.get("cleared_gate")
    current = STAGES_BY_NAME.get(state.get("stage")) if state.get("stage") else None

    while True:
        if current is not None:
            gate = pending_gate(current, mode, after_gate=cleared_gate)
            if gate is not None:
                job_state.park(work_dir, gate, stage=current.name)
                _emit(on_event, "gate", stage=current.name, gate=gate)
                return Result(status=STATUS_GATE, stage=current.name, gate=gate,
                              completed=completed)

        stage = next_stage_after(current.name if current else None)
        if stage is None:
            job_state.update(work_dir, awaiting=None, cleared_gate=None)
            _emit(on_event, "pipeline_done")
            return Result(status=STATUS_DONE, stage=current.name if current else None,
                          completed=completed)

        ok, reason = run_stage(stage, work_dir, base_dir, runner, guard_settings, on_event,
                               passthrough=passthrough)
        if not ok:
            job_state.fail(work_dir, reason)
            return Result(status=STATUS_FAILED, stage=stage.name, reason=reason,
                          completed=completed)

        mark_stage_done(work_dir, stage.name)
        completed.append(stage.name)
        current = stage
        cleared_gate = None


def mark_stage_done(work_dir, stage_name):
    """Record `stage_name` as the last completed stage in job_state.json.

    Used both by advance() after it runs a stage itself, and by callers that
    re-ran a stage's script directly (a rework/redo outside advance()) -- so
    job_state.json's position stays honest and the next advance() resumes
    right after `stage_name` instead of trusting a stale, earlier position.
    """
    if stage_name not in STAGES_BY_NAME:
        raise ValueError(f"unknown stage: {stage_name}")
    job_state.reset_attempts(work_dir, stage_name)
    job_state.update(work_dir, stage=stage_name, last_error=None, cleared_gate=None)


def approve(work_dir):
    """Clear the current gate so the next advance() continues past it."""
    state = job_state.load(work_dir)
    gate = state.get("awaiting")
    job_state.update(work_dir, awaiting=None, cleared_gate=gate)
    return gate


def rewind_to(work_dir, stage_name):
    """Send the job back so `stage_name` runs again on the next advance().

    A human choosing to redo a stage resets its retry budget: their change is
    a fresh attempt, not a continuation of the one that failed.
    """
    if stage_name not in STAGES_BY_NAME:
        raise ValueError(f"unknown stage: {stage_name}")
    index = STAGE_NAMES.index(stage_name)
    previous = STAGE_NAMES[index - 1] if index > 0 else None
    job_state.reset_attempts(work_dir, stage_name)
    job_state.update(work_dir, stage=previous, awaiting=None, cleared_gate=None,
                     last_error=None)
    return previous
