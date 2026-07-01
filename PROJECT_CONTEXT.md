# Project Context - ContentAutomation_shorts

Last updated: 2026-07-02
Current base branch: `main`
Repository: `SanhoLee/ContentAutomation_shorts`
Primary deployment target: AWS Lightsail, expected path `~/brain50`

## Purpose

This repository creates Korean YouTube Shorts through a staged AI pipeline controlled from Telegram. The product direction is stability first: the user should be able to start production from Telegram and recover from non-critical failures without losing the whole job.

The content target is older Korean viewers, often 50+. Generated scripts should avoid stiff expert language, explain medical/technical terms in plain Korean, and keep claims cautious when direct PubMed evidence is weak.

## High-Level Pipeline

The pipeline is split into `dev` and `prod` environments with the same shape:

1. Script generation: `sh/0_script.sh` -> `src/0_script.py`
2. TTS: `sh/1_tts.sh` -> `src/1_tts.py`
3. Caption: `sh/1_caption.sh` -> `src/2_caption.py`
4. B-roll: `sh/1_broll.sh` -> `src/3_broll*.py`
5. Render: `sh/2_render.sh`
6. YouTube upload: `sh/3_upload.sh` -> `src/4_upload.py`
7. Telegram approval workflow: `src/telegram_bot.py`

Outputs are job-scoped under `dev/data/work/{JOB_ID}/` and `prod/data/work/{JOB_ID}/`. Final videos go to each environment's configured output directory.

## Current Stabilization State

The main branch now includes the Lightsail/Telegram stabilization work plus newer fixes:

- Telegram `/set` prints major runtime config and saved overrides.
- Dev Stage 0 runtime config is centralized in `dev/src/script_runtime.py`.
- Stage 0 script length targets are initialized before prompt/log/trim usage.
- web_search is bounded and optional: failure/timeout logs and continues without retry.
- Caption alignment uses sequential Whisper word timestamp consumption plus `CAPTION_OFFSET_SEC=-0.15`.

Recent relevant PRs: #34, #35, #36, #37, #38.

## Telegram Bot Workflow

The Telegram bot supports approval-first and automatic workflows.

Useful commands:

- `/run topic`: approval-based pipeline from a direct topic
- `/run_auto topic`: full pipeline without approval gates
- `/trend keyword`: generate candidate topics
- `/pick 1`: select a trend candidate
- `/set`: print current major runtime config
- `/set font_size=22 margin_v=60 margin_h=12 web=off`: save runtime overrides
- `/approve`: approve current stage
- `/edit`: edit current text artifact when applicable
- `/rerun tts|caption|broll`: regenerate a specific stage
- `/render font_size=22 margin_v=60`: render with custom caption config
- `/status`: inspect current state
- `/cancel`: cancel current job state

Long-running work runs in background threads. While a stage is running, other inputs are ignored except `/status`. Inline buttons carry stage tokens and stale buttons are rejected.

## Topic and Script Generation

`src/0_script.py` supports direct topics, trend candidates, PubMed lookup, bounded web_search, feedback insights, and cautious fallback when PubMed has no direct result.

Important settings in dev:

- `MAX_TOKENS=4000`
- `TARGET_DURATION_SEC=80`
- `ATEMPO=1.10`
- `CHARS_PER_SEC=4.5`
- `ENABLE_WEB_RESEARCH=true`
- `WEB_RESEARCH_TIMEOUT=60`
- `WEB_RESEARCH_MAX_USES=3`
- `WEB_RESEARCH_MAX_TOKENS=900`
- `WEB_RESEARCH_MAX_TOOL_TURNS=2`

web_search is a supplement, not a required production step. It should not retry on timeout because the server may already be processing the request and retrying can create duplicate cost.

## Caption Timing

`dev/src/2_caption.py` uses `caption_script.txt` for display text and faster-whisper word timestamps from `voice.wav`. Current alignment consumes the word timeline sequentially rather than globally snapping every line by total syllable ratio. `CAPTION_OFFSET_SEC=-0.15` shifts generated SRT captions slightly earlier because captions were perceived as lagging the voice.

If captions still lag, tune `CAPTION_OFFSET_SEC` more negative, for example `-0.20`. If captions appear too early, move toward `0`.

## Lightsail Runtime

Expected server path:

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

Secrets are expected in `dev/secrets.sh` and/or `prod/secrets.sh`. Do not commit secrets.

Typical variables:

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
export ANTHROPIC_API_KEY="..."
export PEXELS_API_KEY="..."
export TTS_BIN=/home/ubuntu/.local/bin/supertonic
export CLAUDE_TIMEOUT=300
```

## External Tools

Expected on Lightsail:

- Python 3.10+
- `ffmpeg`
- `ffprobe`
- `supertonic` CLI, usually `/home/ubuntu/.local/bin/supertonic`
- faster-whisper dependencies for caption timestamp extraction
- network access to Telegram Bot API, Anthropic API, PubMed, Google/YouTube suggestion endpoints, Pexels, YouTube upload APIs

## Key Files To Read First In A New Cloud Thread

1. `HANDOFF.md`
2. `KNOWN_ISSUES.md`
3. `ENVIRONMENT_CAPTURE.md`
4. `README.md`
5. `docs/usage/telegram-bot.md`
6. `dev/src/telegram_bot.py`
7. `dev/src/0_script.py`
8. `dev/src/script_runtime.py`
9. `dev/src/2_caption.py`
10. `dev/src/1_tts.py`
