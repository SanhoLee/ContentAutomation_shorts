# Cloud Thread Handoff

Last updated: 2026-08-01
Current base branch: `main`
Repository: `SanhoLee/ContentAutomation_shorts`
Primary runtime: AWS Lightsail at `~/brain50`

## Immediate Context

This project is a Lightsail-hosted Korean Shorts automation pipeline, driven interactively today via a Slack bot. The current priority is production stability: automated flows should keep moving unless a required step truly cannot continue (see `KNOWN_ISSUES.md`).

The longer-term direction is a fully unattended, scheduled content loop (see "North star" in `CLAUDE.md`); the goal-driven planner below is the piece that removes the human topic-selection step, but end-to-end scheduling is not wired up yet.

Start new cloud-agent work from `main` unless the user explicitly names another branch. Create short-lived branches as `codex/{description}` or `claude/{description}` and open draft PRs by default.

> `dev/src` was reorganized into `common/`, `youtube/`, `instagram/` subfolders (commit `66c4a6c`, 2026-07-26). Any older doc or memory referencing `dev/src/0_script.py` flat-style is stale — see the module map below.

## Repository Shape

`dev/src/`:

- `common/` — content-type-agnostic: `0_script.py` (2-stage script generation), `0_topic_plan.py` + `objective_planner.py` (goal-driven auto topic planner), `evidence_probe.py` / `trend_probe.py` (PubMed/trend evidence guards), `pipeline_flow.py` (single source of truth for stage order), `pipeline_orchestrator.py`, `run_pipeline.py` (bot-free CLI), `stage_guard.py` (deterministic between-stage checks), `script_runtime.py` (Stage 0 runtime config, centralize new knobs here), `slack_bot.py`.
- `youtube/` — render-pipeline stages: `1_tts.py`, `2_caption.py`, `3_broll.py` / `3b_retry_broll.py`, `4_upload.py`, `6_youtube_feedback.py`, `broll_policy.py`, `caption_style.py`, `frame_style.py`, `korean_grammar.py`, `classify_uploads_by_category.py`.
- `instagram/` — card-content skeleton (`card_content.py`, `card_render.py`, `publish.py`, `run.py`), not yet in production use.

## Recent Major Work (since 2026-07-02)

Grouped by theme, newest first. See `git log --oneline` for the full list (~130 commits) and individual commit messages for detailed rationale — this repo's commit hygiene is good and often the best source of "why".

- **Retention / hook structure** (`7c30586`, 2026-08-01): Stage 2 prompt no longer resolves the hook's tension at Scene 2 with a generic empathy question; Scene 1 now ends unresolved (`hook_open_loop`), empathy landing moves to Scene 3+, a binary comment-bait question was added at Scene 8/9, titles can no longer carry hashtags or reveal the answer, dev `TARGET_DURATION_SEC` dropped 80→55s. Driven by real retention-curve data (3.6s→8s drop-off of 19–41 points on two published shorts).
- **Evidence-driven topic selection** (`dfe7b9b`, 2026-07-31): `evidence_probe.py` (PubMed query ladder with Hangul/breadth/survival guards) and `trend_probe.py` (multilingual trend ladder with a channel-vocabulary drift gate) fix two silent failures — a real paper being dropped after one failed query, and a collapsed Korean→"ldl" query silently poisoning evidence. `allow_no_pubmed` is no longer hardcoded `True`; needs `ALLOW_NO_PUBMED=1` to bypass an exhausted ladder.
- **Korean caption segmentation rewrite** (`213f3f1`, 2026-07-31): grammar-aware line splitting ported into the caption splitter — particles/endings never separate from their preceding word.
- **Pipeline as a stage graph** (`5e00bde`, 2026-07-30): stage order now lives in one place (`pipeline_flow.py`); execution mode is purely which gates it stops at (`full_gate` / `review` / `auto` — see README "실행 모드"). Cut human intervention to two gates for normal production.
- **B-roll liveliness from source, not re-timing** (`4535197`, 2026-07-29): clips always play at native speed; slow-motion/timelapse/static-long-take clips are filtered out at selection instead of sped up in render.
- **Goal-driven planner hardening** (`e0889b0`, `9619ade`, 2026-07-28/29): fixed decision-threshold (55.0) and confidence-threshold (0.6) being dead/unreachable gates; both now computed per-job from an observed-history percentile (`CLAUDE_SELECTION_PERCENTILE` / `CLAUDE_CONFIDENCE_PERCENTILE`), documented in `docs/design/objective-driven-content-planner.md`.
- **Seed Interpreter + dev reorg + Instagram skeleton** (`5251c3e`, `66c4a6c`, `8c6fdd3`, 2026-07-26): added a Haiku-based seed-interpretation stage to the topic planner, reorganized `dev/src` into `common/youtube/instagram`, and scaffolded (not yet production) Instagram card content.
- **Objective-driven Shorts planning** (`cbbb1a0` et al., 2026-07-20 onward): the entire goal-driven auto-planner — see `docs/design/objective-driven-content-planner.md` for the full operating contract, this is the single most detailed doc in the repo for that subsystem.
- **Slack bot** (`c8cba65` et al., 2026-07-19): full Slack approval workflow — see `docs/usage/slack-bot.md`. It became the only driver on 2026-08-08 when the Telegram bot was deleted.
- **YouTube performance feedback loop** (`44edca0`, `ab58660`, 2026-07-18): adaptive analytics-based feedback into Stage 1/2 — see `docs/usage/youtube-feedback.md`.
- **Caption/frame styling** (multiple, 2026-07-04 to 07-15): configurable caption style presets, ASS conversion, framed Shorts layout with top/bottom safe-zone presets, auto-generated frame headers.

## Current Production Flow

1. Topic input: direct topic string, `topic.json`, trend mode, or `run_goal.sh` (goal-driven planner, no human topic pick).
2. `dev/src/common/0_script.py` — 2-stage script generation (Stage 1 Haiku strategy, Stage 2 Sonnet script), PubMed via `evidence_probe.py`, optional bounded web_search.
3. `dev/src/youtube/1_tts.py` — TTS, produces `voice.wav`.
4. `dev/src/youtube/2_caption.py` — grammar-aware line split + Whisper timestamps, produces `subs.srt`.
5. `dev/src/youtube/3_broll.py` — Pexels B-roll collection.
6. Render (ffmpeg).
7. `dev/src/youtube/4_upload.py` — YouTube upload.
8. `dev/src/common/slack_bot.py` — approval workflow and orchestration (`/run`, `/run_review`, `/run_auto`).

Between every stage, `dev/src/common/stage_guard.py` runs a deterministic, Claude-free check; one bounded retry on failure, then stop and surface the reason (no infinite retries — see `KNOWN_ISSUES.md`).

Prod mirrors the same structure but stabilization work targets dev first. Mirror dev/prod only when the user asks or when behavior is clearly production-ready.

## Server Commands

After merging/pulling latest on Lightsail:

```bash
cd ~/brain50
git pull
./deploy/lightsail/restart_slack_service.sh
```

Logs:

```bash
./deploy/lightsail/logs_slack_service.sh
```

If a service was disabled:

```bash
./deploy/lightsail/install_slack_service.sh
```

## Important Files To Read First

1. `PROJECT_CONTEXT.md`
2. `KNOWN_ISSUES.md`
3. `docs/design/objective-driven-content-planner.md` (if touching topic selection/planning)
4. `README.md`
5. `docs/usage/slack-bot.md`
6. `dev/src/common/slack_bot.py`
7. `dev/src/common/0_script.py`
8. `dev/src/common/script_runtime.py`
9. `dev/src/youtube/2_caption.py`
10. `dev/src/youtube/1_tts.py`

## Current User Preferences

- Korean responses preferred for conversation/status; code identifiers and structural comments stay in English (`CLAUDE.md`).
- Stability and production continuity beat theoretical cleanliness — do not add error paths that halt unattended runs over recoverable conditions.
- Avoid one-off patches that only reveal the next missing global or runtime error; centralize Stage 0 runtime knobs in `script_runtime.py`.
- Do not let optional web_search/PubMed enrichment block production.
- Keep changes narrow, but refactor when scattered state is the root cause (e.g. the `common/youtube/instagram` reorg, the stage-graph refactor).
- Update docs briefly when behavior or operational commands change — this file, `PROJECT_CONTEXT.md`, and `KNOWN_ISSUES.md` had drifted ~1 month stale before this update; keep the drift smaller going forward.
- No unprompted `git commit`/`push` — only on explicit request or an established routine (commit → push → restart dev bots).

## PR Workflow For Codex/Claude Cloud

1. `git fetch origin main`
2. Start from `origin/main`: `git switch -c codex/{short-description} origin/main` (or `claude/{short-description}`)
3. Inspect scope with `git status -sb` and `git diff` before staging.
4. Stage explicit files only.
5. Commit with a terse message explaining *why*, not just what.
6. Push the branch.
7. Open a draft PR with: what changed, why, impact, validation.

## Validation Checklist

Use the narrowest relevant checks first:

```bash
python3 -m py_compile dev/src/common/0_script.py dev/src/common/slack_bot.py
python3 -m py_compile dev/src/youtube/2_caption.py
git diff --check
```

For all dev Python:

```bash
python3 -m compileall -q dev/src
```

Validate shell scripts when touched:

```bash
bash -n dev/sh/*/*.sh deploy/lightsail/*.sh
```

Run the relevant pytest files (each test inserts `dev/src` onto `sys.path` manually):

```bash
python3 -m pytest tests/ -q
```
