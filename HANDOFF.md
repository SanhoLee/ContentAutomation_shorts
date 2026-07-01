# Cloud Thread Handoff

Last updated: 2026-07-02
Current base branch: `main`
Repository: `SanhoLee/ContentAutomation_shorts`
Primary runtime: AWS Lightsail at `~/brain50`

## Immediate Context

This project is a Lightsail-hosted Korean Shorts automation pipeline controlled through Telegram. The current priority is production stability: `/run_auto` and the staged Telegram workflow should keep moving unless a required production step truly cannot continue.

Start new Codex Cloud work from `main` unless the user explicitly names another branch. Create short-lived branches as `codex/{description}` and open draft PRs by default.

## Recent Stabilization PRs To Know

- PR #34 `Show telegram config summary`: `/set` now prints major runtime config instead of only saying defaults are used. Dev config was tuned for longer output.
- PR #35 `Restore dev script length targets`: restored `total_chars`, `prompt_target_chars`, and `min_scenes_estimate` so Stage 0 does not fail before generation.
- PR #36 `Centralize dev script runtime settings`: added `dev/src/script_runtime.py` to centralize Stage 0 env defaults and missing globals.
- PR #37 `Bound dev web research cost`: bounded web_search with `WEB_RESEARCH_TIMEOUT=60`, `WEB_RESEARCH_MAX_USES=3`, `WEB_RESEARCH_MAX_TOKENS=900`, `WEB_RESEARCH_MAX_TOOL_TURNS=2`; web_search failure continues without retry.
- PR #38 `Improve caption timing alignment`: added `CAPTION_OFFSET_SEC=-0.15` and changed caption timing to sequentially consume Whisper word timestamps.

## Current Production Flow

1. `dev/sh/0_script.sh` -> `dev/src/0_script.py`: topic strategy, PubMed, optional bounded web_search, script/meta generation.
2. `dev/sh/1_tts.sh` -> `dev/src/1_tts.py`: TTS plus `tts_script.txt` and `caption_script.txt` generation.
3. `dev/sh/1_caption.sh` -> `dev/src/2_caption.py`: faster-whisper word timestamps, sequential caption alignment, SRT output.
4. `dev/sh/1_broll.sh` -> B-roll collection.
5. `dev/sh/2_render.sh`: ffmpeg render with progress file.
6. `dev/sh/3_upload.sh`: YouTube upload.
7. `dev/src/telegram_bot.py`: Telegram workflow and `/run_auto` orchestration.

Prod mirrors the same structure but recent stabilization has mostly targeted dev. Mirror dev/prod only when the user asks or when the behavior is clearly production-ready.

## Server Commands

After merging/pulling latest on Lightsail:

```bash
cd ~/brain50
git pull
./deploy/lightsail/restart_telegram_service.sh dev
```

Logs:

```bash
./deploy/lightsail/logs_telegram_service.sh dev
```

If service was disabled:

```bash
./deploy/lightsail/install_telegram_service.sh dev
```

## Important Files To Read First

1. `PROJECT_CONTEXT.md`
2. `KNOWN_ISSUES.md`
3. `ENVIRONMENT_CAPTURE.md`
4. `README.md`
5. `docs/usage/telegram-bot.md`
6. `dev/src/telegram_bot.py`
7. `dev/src/0_script.py`
8. `dev/src/script_runtime.py`
9. `dev/src/2_caption.py`
10. `dev/src/1_tts.py`

## Current User Preferences

- Korean responses preferred.
- Stability and production continuity beat theoretical cleanliness.
- Avoid one-off patches that only reveal the next missing global or runtime error.
- Do not let optional web_search block production.
- Make Telegram errors actionable and concise.
- Do not spam Telegram with transient internal errors.
- Keep changes narrow, but refactor when scattered state is the root cause.
- Update docs briefly when behavior or operational commands change.

## PR Workflow For Codex Cloud

1. `git fetch origin main`
2. Start from `origin/main`: `git switch -c codex/{short-description} origin/main`
3. Inspect scope with `git status -sb` and `git diff` before staging.
4. Stage explicit files only.
5. Commit with a terse message.
6. Push the branch.
7. Open a draft PR with: what changed, why, impact, validation.

`gh` auth may be expired in this environment. Prefer the GitHub app connector for PR creation when available.

## Validation Checklist

Use the narrowest relevant checks first:

```bash
python -m py_compile dev\src\0_script.py dev\src\telegram_bot.py
python -m py_compile dev\src\2_caption.py
git diff --check
```

For all dev Python:

```bash
python -m compileall -q dev\src
```

On Linux/Cloud, also validate shell scripts when touched:

```bash
bash -n dev/sh/*.sh prod/sh/*.sh deploy/lightsail/*.sh
```

On Windows Codex, wildcard expansion and console encoding can be awkward. Use explicit file lists and set `PYTHONIOENCODING=utf-8` for Korean output tests.
