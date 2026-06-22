# Project Context - ContentAutomation_shorts

Last updated: 2026-06-22
Branch: `codex/lightsail-stability`
Repository: `SanhoLee/ContentAutomation_shorts`
Primary deployment target: AWS Lightsail, expected path `~/brain50`

## Purpose

This repository is a Shorts automation pipeline for creating Korean YouTube Shorts with a staged human-approval workflow. The current product direction is stability first, especially for a Telegram-driven workflow that can run on Lightsail while the user's local machine is off.

The content target is older Korean viewers, often 50+. Generated scripts should avoid stiff expert language, explain medical/technical terms in plain Korean, and keep claims cautious when direct PubMed evidence is weak.

## High-Level Pipeline

The pipeline is split into dev/prod environments with the same structure:

- `dev/`: development runtime and data
- `prod/`: production runtime and data
- `deploy/systemd/`: systemd service unit files
- `deploy/lightsail/`: service install/restart/log/stop helper scripts
- `docs/usage/`: existing usage and operations documentation

Main stages:

1. Script generation: `sh/0_script.sh` -> `src/0_script.py`
2. TTS: `sh/1_tts.sh` -> `src/1_tts.py`
3. Caption: `sh/1_caption.sh` -> `src/2_caption.py`
4. B-roll: `sh/1_broll.sh` -> `src/3_broll*.py`
5. Render: `sh/2_render.sh`
6. YouTube upload: `sh/3_upload.sh` -> `src/4_upload.py`
7. Telegram approval workflow: `src/telegram_bot.py`

Outputs are job-scoped under:

- `dev/data/work/{JOB_ID}/`
- `prod/data/work/{JOB_ID}/`
- final videos in `data/output/output_{JOB_ID}.mp4`

## Current Branch State

The branch `codex/lightsail-stability` has accumulated the current stabilization work. Important recent commits include:

- `3c0b6b1 fix: suppress transient telegram polling noise`
- `06df009 feat: announce telegram bot lifecycle`
- `410db2b feat: report render progress in telegram`
- `9ae32a0 fix: enforce staged telegram approvals`
- `f79398f feat: guard telegram actions while running`
- `b672e34 fix: resolve tts binary in service environment`
- `463d3fb feat: add editable telegram approvals`
- `73915a5 fix: harden script generation failures`

The branch was pushed to origin after each stabilization step.

## Telegram Bot Workflow

The Telegram bot supports an approval-first workflow. Long-running work is run in background threads while the bot keeps polling.

Important behavior:

- While a stage is running, other buttons/commands are ignored except `/status`.
- The user receives a `currently running` style message if they tap during work.
- Inline buttons carry the stage they were created for, for example `approve:await_caption_approval`.
- Old buttons from previous stages are rejected if they do not match the current `job["stage"]`.
- Each approval stage can move back to a previous stage where it makes sense.
- `전체 취소` means cancel the whole current job state.

Useful Telegram commands:

- `/run topic`: start approval-based pipeline from a direct topic
- `/trend keyword`: generate candidate topics
- `/pick 1`: select a trend candidate
- `/approve`: approve current stage from text command
- `/edit`: edit current text artifact when applicable
- `/rerun tts|caption|broll`: regenerate a specific stage
- `/render font_size=22 margin_v=180`: render with custom caption config
- `/status`: inspect current state
- `/cancel`: cancel whole job state

Editable artifacts:

- `script.txt`: editable before TTS, and can be revisited from TTS approval
- `subs.srt`: editable before render
- `video_meta.json`: editable before YouTube upload

Non-text stages use regeneration or stage-back buttons instead of direct edit.

## Topic and Script Generation

`src/0_script.py` supports:

- Direct topic input
- Trend seed mode with Google/YouTube suggestions
- Trend choice mode via `trend_candidates.json`
- PubMed lookup and fallback handling

Important prompt policy:

- Do not force all numbers into Korean spelling.
- Keep important research numbers as Arabic numerals when appropriate.
- Adjust spacing/particles to avoid awkward TTS, e.g. `오메가3는`, `50대 이상은`, `퍼센트`.
- Explain expert terms in simple language for older viewers.
- If PubMed has no direct results, continue generation using reliable general medical knowledge, but do not invent study numbers, sample sizes, or exact results.

PubMed status is written to:

- `data/work/{JOB_ID}/pubmed_status.json`

## Lightsail Runtime

Expected path on server:

```bash
~/brain50
```

Service helpers:

```bash
./deploy/lightsail/install_telegram_service.sh dev
./deploy/lightsail/restart_telegram_service.sh dev
./deploy/lightsail/logs_telegram_service.sh dev
./deploy/lightsail/stop_telegram_service.sh dev
```

Systemd unit names:

- `brain50-telegram-dev.service`
- `brain50-telegram-prod.service`

Bot startup behavior:

- Sends a welcome message and help text to Telegram.
- On SIGTERM/SIGINT, sends `bye bye` shutdown notice.
- Transient Telegram polling errors are no longer sent to Telegram repeatedly; they are logged server-side.

## Required Secrets

Secrets are expected in `dev/secrets.sh` and/or `prod/secrets.sh`. Do not commit secrets.

Typical variables:

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
export ANTHROPIC_API_KEY="..."
export PEXELS_API_KEY="..."
export TTS_BIN=/home/ubuntu/.local/bin/supertonic
```

`TTS_BIN` is optional if `supertonic` is discoverable via PATH, but on systemd it is safer to set explicitly.

## External Tools

Expected on Lightsail:

- Python 3.10+
- `ffmpeg`
- `ffprobe`
- `supertonic` CLI, usually `/home/ubuntu/.local/bin/supertonic`
- network access to Telegram Bot API, Anthropic API, PubMed, Google/YouTube suggestion endpoints, Pexels, YouTube upload APIs

## Key Files to Read First in a New Cloud Thread

1. `HANDOFF.md`
2. `KNOWN_ISSUES.md`
3. `PROJECT_CONTEXT.md`
4. `ENVIRONMENT_CAPTURE.md`
5. `docs/usage/telegram-bot.md`
6. `dev/src/telegram_bot.py`
7. `dev/src/0_script.py`
8. `dev/sh/2_render.sh`
9. `dev/src/1_tts.py`