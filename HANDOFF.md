# Cloud Thread Handoff

Last updated: 2026-06-22
Current branch: `codex/lightsail-stability`
Latest pushed commit at handoff time: `3c0b6b1 fix: suppress transient telegram polling noise`

## Immediate Context

The user is migrating from a local Codex desktop thread to a cloud thread and wants context preserved. Continue from this branch unless the user explicitly asks to switch branches.

The project is a Lightsail-hosted Korean Shorts automation pipeline controlled through Telegram. Recent work has focused on making the pipeline stable enough for step-by-step human approval.

## What Was Recently Built

### Environment and artifacts

- dev/prod directories are separated.
- Work artifacts are under `data/work/{JOB_ID}/`.
- Final outputs are under `data/output/`.
- Stage scripts can be run independently using the same `JOB_ID`.

### Prompt and script generation

- Korean prompt instructions were rewritten to be natural Korean.
- Direct idea mode and trend candidate mode were added.
- Trend mode checks Google/YouTube style suggestions and stores candidates in `trend_candidates.json`.
- PubMed no-result handling now logs `pubmed_status.json` and continues generation with caution.
- Claude timeout/retry was hardened.
- Prompt no longer forces every number into Korean spelling.
- Prompt asks for easier terms for older Korean viewers.

### Telegram workflow

- Telegram bot can start `/run`, `/trend`, `/pick` workflows.
- Approval gates exist at script, TTS, caption, B-roll, render config, final render, metadata/upload.
- Inline buttons are used for approve/edit/rerun/back/cancel actions.
- Long-running work runs in background threads; while busy, other inputs are ignored except `/status`.
- Buttons contain stage metadata to prevent stale buttons from approving the wrong current stage.
- User can go back to previous stages and re-approve.
- TTS approval stage can go back to script editing so the user can fix spacing/numbers/particles and regenerate TTS.
- Text artifacts can be edited through `/edit` or `수정` button by uploading replacement files/text.

### Lightsail systemd operation

- Service files exist under `deploy/systemd/`.
- Helper scripts exist under `deploy/lightsail/`:
  - `install_telegram_service.sh`
  - `restart_telegram_service.sh`
  - `logs_telegram_service.sh`
  - `stop_telegram_service.sh`
- Bot sends welcome + help on startup.
- Bot sends bye bye on SIGTERM/SIGINT.
- Transient Telegram polling errors are suppressed from Telegram and logged server-side.

### Render progress

- `2_render.sh` now writes ffmpeg progress to `render_progress.txt`.
- Telegram reads the progress file and sends start/25/50/75/complete messages.

## Current Recommended Server Commands

After pulling latest branch on Lightsail:

```bash
cd ~/brain50
git pull
./deploy/lightsail/restart_telegram_service.sh dev
```

If service was disabled with stop script:

```bash
cd ~/brain50
./deploy/lightsail/install_telegram_service.sh dev
```

Logs:

```bash
./deploy/lightsail/logs_telegram_service.sh dev
```

TTS path check:

```bash
which supertonic
ls ~/.local/bin/supertonic
```

If needed in `dev/secrets.sh`:

```bash
export TTS_BIN=/home/ubuntu/.local/bin/supertonic
```

## Important Files

Read these before changing behavior:

- `dev/src/telegram_bot.py`
- `prod/src/telegram_bot.py`
- `dev/src/0_script.py`
- `prod/src/0_script.py`
- `dev/src/1_tts.py`
- `prod/src/1_tts.py`
- `dev/sh/2_render.sh`
- `prod/sh/2_render.sh`
- `docs/usage/telegram-bot.md`
- `docs/usage/environment.md`
- `KNOWN_ISSUES.md`

## Current User Preferences

- Korean responses preferred.
- Keep changes practical and stability-first.
- Do not over-automate approval flow yet; approval gates are intentional during development.
- For frontend/UI-like Telegram workflows, use buttons where they reduce input mistakes.
- Make error messages actionable and user-understandable.
- Do not spam Telegram with transient internal errors.
- Keep all changes mirrored in dev/prod unless intentionally environment-specific.
- When changing behavior, update docs briefly.

## How to Continue in Cloud

1. Confirm branch and latest commit.
2. Pull remote state.
3. Read `PROJECT_CONTEXT.md`, `KNOWN_ISSUES.md`, and this file.
4. If debugging a user screenshot/log, identify whether the error is pipeline-stage failure, service/runtime issue, or transient Telegram/network issue.
5. Prefer narrow fixes with `py_compile` and `git diff --check`.
6. If shell scripts are touched, run `bash -n` on a Linux/WSL environment if available. Local Windows thread previously lacked WSL.
7. Commit and push to `codex/lightsail-stability` unless user asks otherwise.

## Last Validation Performed Before Handoff

For recent changes, the following checks have been used repeatedly:

```bash
python -m py_compile dev/src/*.py prod/src/*.py
python -m py_compile dev/src/telegram_bot.py prod/src/telegram_bot.py
git diff --check
```

Note: In the local Windows Codex environment, wildcard expansion required passing explicit file lists or using `git ls-files`. `bash -n` could not run because WSL/bash was not available.