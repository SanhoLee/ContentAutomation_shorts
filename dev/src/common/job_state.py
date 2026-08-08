"""Durable per-job pipeline state stored next to the job's artifacts.

Job state used to live only inside each bot's in-memory chat dict (and its
`state.json`), which meant nothing except that bot process could tell where a
job stood.  An unattended loop needs the opposite: any driver -- the Slack
bot, a cron-launched CLI -- must be able to pick up a job,
see which stage it reached, whether it is parked at a human gate, and resume.

So the state lives in the job's own work directory as `job_state.json`.  This
module owns that file and nothing else: no transport, no subprocess, no
knowledge of the stage graph.  Callers pass the work directory in; every
function returns the resulting state dict.

Writes go through a temp file + os.replace so a process killed mid-write
leaves the previous state intact rather than a truncated file.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

STATE_FILENAME = "job_state.json"

# Run modes differ only in which gates they honour -- see pipeline_flow.
MODE_AUTO = "auto"            # no gates; fully unattended
MODE_REVIEW = "review"        # script review + final confirm only (2-gate)
MODE_FULL_GATE = "full_gate"  # stop after every stage (legacy /run)

MODES = (MODE_AUTO, MODE_REVIEW, MODE_FULL_GATE)


def default_state():
    """A fresh state dict.  Every field the pipeline reads must exist here."""
    return {
        "stage": None,        # name of the last completed stage, None before the first
        "mode": MODE_REVIEW,
        "awaiting": None,     # gate name the job is parked at, None while runnable
        "cleared_gate": None,  # gate a human just approved, so we resume after it
        "attempts": {},       # stage name -> how many times it has been run
        "settings": {},       # render/caption knobs carried across stages
        "last_error": None,
    }


def state_path(work_dir):
    return Path(work_dir) / STATE_FILENAME


def load(work_dir):
    """Read the job's state, falling back to defaults for missing/corrupt files.

    A damaged state file must not strand a job: an unattended run would have
    no one to fix it.  Unknown keys are preserved, missing keys are filled in.
    """
    path = state_path(work_dir)
    state = default_state()
    if not path.exists():
        return state
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return state
    if not isinstance(stored, dict):
        return state
    state.update(stored)
    if not isinstance(state.get("attempts"), dict):
        state["attempts"] = {}
    if not isinstance(state.get("settings"), dict):
        state["settings"] = {}
    return state


def save(work_dir, state):
    """Atomically persist `state`; returns it so callers can chain."""
    path = state_path(work_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return state


def update(work_dir, **fields):
    """Merge `fields` into the stored state."""
    state = load(work_dir)
    state.update(fields)
    return save(work_dir, state)


def set_mode(work_dir, mode):
    if mode not in MODES:
        raise ValueError(f"mode must be one of: {', '.join(MODES)}")
    return update(work_dir, mode=mode)


def attempts_for(work_dir, stage):
    return int(load(work_dir).get("attempts", {}).get(stage, 0))


def record_attempt(work_dir, stage):
    """Increment and persist the run count for `stage`; returns the new count."""
    state = load(work_dir)
    attempts = state.setdefault("attempts", {})
    attempts[stage] = int(attempts.get(stage, 0)) + 1
    save(work_dir, state)
    return attempts[stage]


def reset_attempts(work_dir, stage=None):
    """Clear the retry budget for one stage, or all stages when `stage` is None.

    Used when a human sends a job back to an earlier stage: the operator's
    intervention is a fresh start, not a continuation of the failed budget.
    """
    state = load(work_dir)
    if stage is None:
        state["attempts"] = {}
    else:
        state.setdefault("attempts", {}).pop(stage, None)
    return save(work_dir, state)


def park(work_dir, gate, stage=None):
    """Park the job at a human gate.  Drivers exit after this; resume() undoes it."""
    fields = {"awaiting": gate}
    if stage is not None:
        fields["stage"] = stage
    return update(work_dir, **fields)


def resume(work_dir):
    """Clear the gate so the job becomes runnable again (human approved)."""
    return update(work_dir, awaiting=None)


def fail(work_dir, reason):
    """Record why the pipeline stopped, without clearing progress."""
    return update(work_dir, last_error=reason)


def is_awaiting(work_dir):
    return load(work_dir).get("awaiting") is not None
