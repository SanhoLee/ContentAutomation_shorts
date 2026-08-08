# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Korean YouTube Shorts automation pipeline for a brain-health channel targeting viewers 50+. It generates scripts with Claude, synthesizes TTS voice, aligns captions, sources B-roll, renders with ffmpeg, and uploads to YouTube — driven via a Slack bot, deployed on an AWS Lightsail instance at `~/brain50`.

Most documentation and in-repo comments are written in Korean, matching the target audience of the content itself. Prefer Korean when writing user-facing script/UX copy; code identifiers and structural comments stay in English.

## North star

The end goal is a fully unattended, scheduled content loop: on a recurring schedule, the system should generate its own seed/topic (via the goal-driven planner), write the script, produce voice/captions/B-roll, render, and publish to YouTube without a human triggering each run. Today the pipeline is driven interactively (Slack approval flows, manual CLI runs); the goal-driven planner (`0_topic_plan.py` / `objective_planner.py`, see `docs/design/objective-driven-content-planner.md`) is the piece that removes the human topic-selection step. Scheduling the whole loop end-to-end (cron/systemd timer → seed → topic → script → render → upload) is not wired up yet — treat it as the direction new automation work should move toward, not an already-solved problem.

## Where to look

This repo already documents itself in depth; read the relevant file rather than duplicating it here:

- `README.md` — full pipeline stage-by-stage walkthrough, module details, environment variables, content strategy.
- `docs/usage/` — task-oriented guides: `basic-usage.md`, `with-job-id.md`, `environment.md`, `slack-bot.md`, `youtube-feedback.md`, `topic-scheduling.md`.
- `docs/design/objective-driven-content-planner.md` — operating contract for the goal-driven auto-planning engine (the North star's topic-selection piece).
- `docs/design/story-types.md` — the four narrative genres, how the mix is enforced on the auto path, and what `USE_STORY_TYPES=0` rolls back.
- `PROJECT_CONTEXT.md` — current stabilization state, recent PRs, key files to read first in a new session.
- `HANDOFF.md` — prior cloud-agent session handoff notes, PR workflow, validation checklist.
- `KNOWN_ISSUES.md` — active risk register; check before touching Slack event handling, caption timing, web_search cost, or Stage 0 runtime settings.
- `ENVIRONMENT_CAPTURE.md` — local/server environment quirks (Windows vs. Lightsail, expected binaries, encoding).

## Repository layout

- `dev/` is the single environment (own `src/`, `sh/`, `config.sh`, `secrets.sh`, `data/`). A parallel `prod/` tree used to exist but fell far enough behind `dev` (it predated the `common/youtube/instagram` split and ran no jobs) that it was deleted; if a production split is needed again, branch it from a validated `dev` rather than reviving the old copy.
- `dev/src/*.py` — pipeline stages and bots; see `README.md` for what each numbered script does.
- `dev/data/work/{JOB_ID}/` — all artifacts for one job (script, voice, captions, B-roll, render) live together, keyed by job ID.
- `dev/data/youtube_feedback.db` — SQLite store of synced YouTube performance data, objectives, candidate plans, and Claude cost logs.
- `deploy/` — systemd unit file and Lightsail install/restart/stop/logs helpers for the Slack bot service.
- `tests/` — pytest tests at the repo root; each test file inserts `dev/src` onto `sys.path` manually (there is no installed package), so new tests should follow the same pattern.

## Working conventions specific to this repo

- Production stability beats theoretical cleanliness: automated flows should keep moving unless a step genuinely cannot continue. Prefer bounded, non-retrying failure handling over retries that could double API cost (see `KNOWN_ISSUES.md`).
- Don't add new scattered `os.environ` parsing in `0_script.py` — centralize runtime knobs in `script_runtime.py` (this has regressed before, see `KNOWN_ISSUES.md`).
- In the goal-driven planner, keep scoring/selection deterministic in Python — Claude proposes and critiques candidates but never makes the final numeric decision (see `docs/design/objective-driven-content-planner.md`). The same rule covers `story_type`: the genre is apportioned in `story_types.pick_story_type()`, and Claude only writes inside the genre it is handed.
- Never commit `secrets.sh`, `.env`, `client_secret*.json`, or `youtube_feedback_token*.json` — these are gitignored and expected per-environment on the Lightsail host.
