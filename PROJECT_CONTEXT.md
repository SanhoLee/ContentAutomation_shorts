# Project Context - ContentAutomation_shorts

Last updated: 2026-08-08
Current base branch: `main`
Repository: `SanhoLee/ContentAutomation_shorts`
Primary deployment target: AWS Lightsail, expected path `~/brain50`

## Purpose

This repository creates Korean YouTube Shorts through a staged AI pipeline, driven interactively today from Slack. The product direction is stability first: production should keep moving and recover from non-critical failures without losing the whole job. The longer-term direction is a fully unattended, scheduled content loop (see `CLAUDE.md` "North star") — the goal-driven planner below is the piece that removes the human topic-selection step, but end-to-end scheduling is not wired up yet.

The content target is older Korean viewers, often 50+. Generated scripts should avoid stiff expert language, explain medical/technical terms in plain Korean, and keep claims cautious when direct PubMed evidence is weak.

The X(Twitter) thread adapter is the one deliberate exception to that audience: it re-writes the same script for 20s-40s readers, in polite Korean (존댓말), with no hashtags and no links, and a lead tweet whose title is written for X rather than reused from the Shorts title. See README "X(Twitter) 업로드 후 자동 게시".

## High-Level Pipeline

The pipeline runs from a single `dev` environment. `dev/src` is split into `common/` (content-type-agnostic), `youtube/` (render pipeline), and `instagram/` (card-content skeleton, not yet in production). A parallel `prod/` tree was deleted on 2026-08-08 after it fell behind this layout and stopped running jobs.

1. Topic selection: direct topic string, `topic.json`, trend mode, or `run_goal.sh` → `src/common/0_topic_plan.py` (goal-driven auto planner, no human pick).
2. Script generation: `sh/common/0_script.sh` → `src/common/0_script.py` (2-stage: Haiku strategy, Sonnet script). Words and `role` only.
3. Scene visuals: `sh/common/scene_visuals.sh` → `src/common/scene_visuals.py`. Runs *after* the script gate on the approved text; re-syncs scene text to `script.txt`, plans `visual`/`visual_query` in one Haiku call, refreshes `content_package.json`.
4. TTS: `sh/youtube/1_tts.sh` → `src/youtube/1_tts.py`.
5. Caption: `sh/youtube/1_caption.sh` → `src/youtube/2_caption.py`.
6. B-roll: `sh/youtube/1_broll.sh` → `src/youtube/3_broll*.py`.
7. Render: `sh/youtube/2_render.sh`.
8. YouTube upload: `sh/youtube/3_upload.sh` → `src/youtube/4_upload.py`.
9. Approval workflow: `src/common/slack_bot.py`.

Stage order is defined once in `src/common/pipeline_flow.py` (`STAGES`); execution mode (`full_gate` / `review` / `auto`) is purely which gates it stops at, not a different code path. Between stages, `src/common/stage_guard.py` runs a deterministic, Claude-free check and allows one bounded retry before stopping and surfacing the reason to a human — no infinite retries.

Outputs are job-scoped under `dev/data/work/{JOB_ID}/`. Final videos go to the configured output directory.

## Current Stabilization State

- Pipeline stage order is centralized (`pipeline_flow.py`); Slack orchestration is driven from it.
- Dev Stage 0 runtime config is centralized in `src/common/script_runtime.py` — avoid scattered `os.environ` parsing in `0_script.py`.
- Goal-driven planner's decision/confidence gates are computed per-job from an observed-history percentile instead of stale fixed thresholds (`docs/design/objective-driven-content-planner.md`).
- `evidence_probe.py` / `trend_probe.py` guard PubMed and trend queries against silent Hangul-collapse and query poisoning before they reach script generation.
- Korean caption line-splitting is grammar-aware (particles/endings never separate from their word).
- B-roll always plays at native speed; slow-motion/timelapse/static clips are filtered at selection, not sped up in render.
- Stage 2 script prompt keeps the hook's tension open past Scene 1 instead of resolving it at Scene 2 (2026-08-01, driven by real retention-curve data).
- Visual planning moved out of Stage 2 into the post-approval `scene_visuals` stage (2026-08-08), so an edited script body no longer leaves `scenes.json`, B-roll queries and the X thread describing the pre-edit text.
- The X thread never auto-posts without an operator-supplied lead image (2026-08-08): `x_poster` holds it (`PhotoPending`) until the photo arrives in Slack, and `/x_post` is the explicit "send it as-is" escape. The stored X token still lacks the `media.write` scope — re-consent in the X Developer Portal is an outstanding operator action (`KNOWN_ISSUES.md` #11).
- web_search is bounded and optional: failure/timeout logs and continues without retry.

See `HANDOFF.md` "Recent Major Work" for the commit-level breakdown; `git log --oneline` has the full history (~130 commits since 2026-07-02, not individually listed here).

## Slack Bot Workflow

The bot supports approval-first and automatic workflows, driven by interactive buttons/home tab as well as slash commands (see `docs/usage/slack-bot.md`).

Useful commands:

- `/run topic`: full gate — approval at every stage
- `/run_review topic`: two gates — script review, then final approval before upload (default for normal production)
- `/run_auto topic`: zero gates — full unattended pipeline
- `/trend keyword`: generate candidate topics
- `/pick 1`: select a trend candidate
- `/set`: print current major runtime config
- `/set font_size=22 margin_v=60 margin_h=12 web=off case=off`: save runtime overrides
- `/approve`: approve current stage
- `/edit`: edit current text artifact when applicable
- `/rerun tts|caption|broll`: regenerate a specific stage
- `/render font_size=22 margin_v=60`: render with custom caption config
- `/status`: inspect current state
- `/cancel`: cancel current job state

- `ENABLE_CASE_RESEARCH`: toggles supplemental Korean case/stat web_search enrichment (default on; `/set case=off` disables it per job).

Long-running work runs in background threads. While a stage is running, other inputs are ignored except `/status`. Inline buttons carry stage tokens and stale buttons are rejected.

## Goal-Driven Auto Planning

`./run_goal.sh {objective} [seed]` runs `src/common/0_topic_plan.py` to pick a topic without a human, score it deterministically in Python (Claude only proposes/critiques, never decides), and hand off to the existing `0_script.py --topic-json` path. Full operating contract — scoring formula, decision/confidence thresholds, evidence probing, exploration randomness — is in `docs/design/objective-driven-content-planner.md`; do not duplicate it here, read that file before touching `objective_planner.py`.

## Scheduled Trend Research and Topic Selection

`sh/common/refresh_topics.sh` runs `topic_candidate_pipeline.py --refresh` (seed pool → Google/YouTube autocomplete → deterministic scoring, no Claude calls) and prints the top 3, optionally posting a Slack card with selection buttons. A human then picks one — `--select-rank N` from the CLI, or `/topics` and a button in Slack.

Selection **records only**; it does not start production. The pick lands in `data/topics/selected.json`, which is the seam for wiring this into `0_topic_plan.py` / `run_pipeline.py` later. `status: "selected"` (a human picked it) is deliberately separate from `consumed` (the pipeline spent it) so `pick_top_eligible()`'s existing auto-path behavior is unchanged.

Candidates are constrained to the channel's **base domain**, defined by the operator in `dev/config/topic_domain.json` and enforced by `topic_domain.py` in two places: risk-factor category seeds are probed anchored (`고혈압` → `치매 고혈압`), and any candidate with no anchor term is rejected as `no_domain_anchor` regardless of score. Without this, `research_categories.json`'s risk-factor keywords reached the suggest API stripped of the `AND cognitive decline` half of their PubMed query, and `심혈관질환 증상` out-scored `뇌 건강 식단` on search-query shape alone. Read `docs/usage/topic-scheduling.md#베이스-분야-설정` before changing weights — `threshold` is calibrated against them (see `KNOWN_ISSUES.md` items 21–22).

The refresh is not registered with any timer yet — systemd timer and crontab recipes are in `docs/usage/topic-scheduling.md`, to be enabled on the server when ready.

## Topic and Script Generation

`src/common/0_script.py` supports direct topics, trend candidates, PubMed lookup (via `evidence_probe.py`'s widening query ladder), bounded web_search, feedback insights, and cautious fallback when PubMed has no direct result.

Important settings in dev (`dev/config.yaml`):

- `MAX_TOKENS=4000`
- `TARGET_DURATION_SEC=55` (dropped from 80 on 2026-08-01; 50–65s is where view-count median peaks per channel analysis)
- `ATEMPO=1.05`
- `CHARS_PER_SEC=4.5`
- `ENABLE_WEB_RESEARCH=true`
- `WEB_RESEARCH_TIMEOUT=60`
- `WEB_RESEARCH_MAX_USES=2`
- `ALLOW_NO_PUBMED`: not hardcoded true anymore — set `ALLOW_NO_PUBMED=1` to continue when the PubMed evidence ladder is fully exhausted.

web_search is a supplement, not a required production step. It should not retry on timeout because the server may already be processing the request and retrying can create duplicate cost.

## Caption Timing

`dev/src/youtube/2_caption.py` uses `script.txt` as the source of display text (grammar-aware line splitting — particles/endings never separate from their word) and faster-whisper word timestamps from `voice.wav` for timing only. Alignment consumes the word timeline sequentially rather than globally snapping every line by total syllable ratio. `CAPTION_OFFSET_SEC=-0.15` shifts generated SRT captions slightly earlier because captions were perceived as lagging the voice.

If captions still lag, tune `CAPTION_OFFSET_SEC` more negative, for example `-0.20`. If captions appear too early, move toward `0`.

## Lightsail Runtime

Expected server path:

```bash
~/brain50
```

Service helpers:

```bash
./deploy/lightsail/install_slack_service.sh
./deploy/lightsail/restart_slack_service.sh
./deploy/lightsail/logs_slack_service.sh
./deploy/lightsail/stop_slack_service.sh
```

Secrets are expected in `dev/secrets.sh`. Do not commit secrets.

Full variable list (required + tuning): `ENVIRONMENT_CAPTURE.md` → "Recommended Lightsail Environment Variables".

## External Tools

Expected on Lightsail:

- Python 3.10+
- `ffmpeg`
- `ffprobe`
- `supertonic` CLI, usually `/home/ubuntu/.local/bin/supertonic`
- faster-whisper dependencies for caption timestamp extraction
- network access to Slack API, Anthropic API, PubMed/Europe PMC, Google/YouTube suggestion endpoints, Pexels, YouTube upload APIs

## Key Files To Read First In A New Cloud Thread

1. `HANDOFF.md`
2. `KNOWN_ISSUES.md`
3. `docs/design/objective-driven-content-planner.md` (if touching topic selection/planning)
4. `README.md`
5. `docs/usage/slack-bot.md`
6. `dev/src/common/slack_bot.py`
7. `dev/src/common/0_script.py`
8. `dev/src/common/script_runtime.py`
9. `dev/src/youtube/2_caption.py`
10. `dev/src/youtube/1_tts.py`
