# Known Issues and Risk Register

Last updated: 2026-08-07
Current base branch: `main`

## Active / Watch Items

### 1. Telegram bot long polling is network-sensitive

Status: mitigated, still expected occasionally in logs.

Observed symptoms:

- `The read operation timed out`
- `Connection reset by peer`
- `Remote end closed connection without response`

Current handling:

- Transient polling errors are no longer sent repeatedly to Telegram.
- They are printed to server logs as warnings and retried with backoff.
- Unknown polling errors are rate-limited by `TELEGRAM_POLL_ERROR_NOTIFY_INTERVAL`, default 1800 seconds.

### 2. Background thread state persistence

Status: improved.

- `STATE_LOCK` protects state writes.
- State is written to a temporary file and atomically replaced via `os.replace`.
- The in-memory `state` dict is still shared between main thread and background tasks; current usage is simple but a queue/single state manager would be safer if workflows become more complex.

### 3. `/approve` text command has no stage token

Status: acceptable.

Inline buttons carry stage tokens and reject stale buttons. Text command `/approve` still approves whatever current stage is in `job["stage"]`. This is intentional as an explicit current-stage command.

### 4. Stage 0 runtime settings can regress if scattered again

Status: recently refactored, still watch.

Recent failures included missing `total_chars` and `ENABLE_WEB_RESEARCH`. The fix is `dev/src/common/script_runtime.py`, which centralizes Stage 0 env defaults and derived values. Avoid reintroducing new global env parsing directly in `dev/src/common/0_script.py`; add new runtime knobs to `script_runtime.py` instead. This has regressed before — see `CLAUDE.md` working conventions.

### 5. web_search cost and timeout behavior

Status: bounded.

Current dev defaults (code default in `script_runtime.py`, overridden in `dev/config.yaml` where noted):

```bash
ENABLE_WEB_RESEARCH=true
WEB_RESEARCH_TIMEOUT=60
WEB_RESEARCH_MAX_USES=2        # dev/config.yaml override (code default is also 2)
WEB_RESEARCH_MAX_TOKENS=900
WEB_RESEARCH_MAX_TOOL_TURNS=2
CASE_RESEARCH_MAX_USES=2
```

web_search is optional. Timeout/tool errors return an empty supplement and script generation continues. It should not retry automatically after timeout because the request may already be processing server-side, creating duplicate cost risk.

### 6. Caption timing may still need empirical tuning

Status: improved, watch in rendered output.

`dev/src/youtube/2_caption.py` uses sequential Whisper word timestamp consumption, grammar-aware Korean line splitting (`korean_grammar.py`, 2026-07-31), and `CAPTION_OFFSET_SEC=-0.15`. If captions still lag, try a slightly more negative offset such as `-0.20`. If captions appear early, move toward `0`. This is a perceptual tuning knob, not a render margin/font setting.

### 7. Render progress is based on ffmpeg progress file

Status: implemented.

`2_render.sh` writes `render_progress.txt` via ffmpeg `-progress`. Telegram/Slack read it and send checkpoints: start, 25%, 50%, 75%, complete. Very short renders may skip intermediate checkpoints.

### 8. TTS CLI path under systemd

Status: mitigated.

`config.sh` prepends `$HOME/.local/bin:/usr/local/bin` to PATH. `1_tts.py` checks `TTS_BIN`, `SUPERTONIC_BIN`, PATH, and common bin directories.

Recommended server setting:

```bash
export TTS_BIN=/home/ubuntu/.local/bin/supertonic
```

### 9. PubMed no-result topics

Status: handled by an evidence ladder, not a single query.

`evidence_probe.py` (2026-07-31) walks full → narrowed → core → category query rungs before giving up, guarded against absurd-breadth and query-poisoning results (see `docs/design/objective-driven-content-planner.md` "근거 확인"). If the full ladder is exhausted, generation continues only when `ALLOW_NO_PUBMED=1` is set — this is no longer hardcoded `True`. `pubmed_status.json` records `ladder_rung` (`full`/`narrowed`/`core`/`category`); watch its distribution — a high share of `category` hits means original queries are too narrow/collapsed and deserves a look upstream (Seed Interpreter query quality).

### 10. Claude API read timeout

Status: mitigated.

- `CLAUDE_TIMEOUT`, default 180 seconds; server tuning often uses 300.
- HTTP 429/5xx may retry inside `CLAUDE_HTTP_RETRIES`.
- Read/connect timeout is not automatically retried to reduce duplicate cost risk.

### 11. Telegram/Slack file editing UX limitations

Status: pragmatic workaround.

Editable artifacts:

- `script.txt`
- `subs.srt`
- `video_meta.json`

The bots send files; the user uploads replacement text/file to overwrite the relevant artifact.

### 12. YouTube upload final state

Status: not heavily exercised in recent local testing.

Upload is expected after final metadata approval. Inspect `dev/src/youtube/4_upload.py` before changing upload behavior.

### 13. Encoding in Windows terminal output

Status: local display issue.

Some Korean text can appear mojibake or fail to print under Windows console encodings. Use `PYTHONIOENCODING=utf-8` or inspect files directly before assuming source corruption. (Also relevant: `trend_probe.py` needed explicit `ie/oe=utf-8` on its Google Suggest requests — the old call site's absence of this produced mojibake Korean and unparseable Japanese silently swallowed behind `errors="replace"`, 2026-07-31.)

### 14. `hook_open_loop` / title-hashtag are warnings, not gates (new, 2026-08-01)

Status: monitor after deploy.

The retention-cliff fix added `missing_hook_open_loop` and `hooky_title_hashtag` to `validate_script()` as warnings only — an unattended run must not stop because the model skipped a field (production-continuity principle above). Check `script_quality.json` `missing_hook_open_loop` frequency after the first batch of jobs on the new prompt; ≥30% means the model isn't reliably constructing the open loop and the prompt needs a firmer example, not a stricter gate.

### 15. `classify_uploads_by_category.py` is a standalone script, not wired into any automated flow

Status: as-designed for now, revisit if it should feed the planner.

Added 2026-07-29 (`dev/src/youtube/classify_uploads_by_category.py`) to classify already-uploaded videos into `research_categories.json` categories via the YouTube API. It is run manually; it does not currently feed `objective_planner.py` or any scheduled job. If category-drift analysis becomes routine, this is a candidate for the "scheduled loop" direction in `CLAUDE.md`'s North star, not an isolated cron job.

### 16. `classify_api_error` misclassified `RefreshError`, masking OAuth re-auth need for days (fixed 2026-08-01)

Status: fixed in code (classification + reason surfacing); recurrence still possible if OAuth consent screen stays in "Testing" publishing status (operational, not code).

`load_credentials()` in `6_youtube_feedback.py` calls `creds.refresh(Request())`, which raises `google.auth.exceptions.RefreshError` when the refresh token is revoked/expired. `classify_api_error()` read `exc.resp.status`, which `RefreshError` doesn't have, so it always fell through to the generic `"API/동기화 실패"` bucket instead of `"인증/권한 실패"`. Symptom: `sync_runs.error_message` read `API/동기화 실패: RefreshError` (now `인증/권한 실패: RefreshError` after the fix) across 16 consecutive `sync_runs` (run_id 23-38, starting 2026-07-26), and goal-based planning jobs (`0_topic_plan.py`) silently landed in `decision=manual_review`, `candidate_count=0` once the cache passed the 7-day `stale_hours` guard — the Slack/Telegram message read like "no candidates for this seed" with no mention of the real cause. An earlier related job on 2026-07-27 hit `sync_status=refresh-failed-cache` (job continued on stale cache, per the production-continuity principle) with the same lack of visibility.

Fix: `classify_api_error` now checks `isinstance(exc, RefreshError)` explicitly; `0_topic_plan.py`'s `cmd_plan()` now enriches `objective.reason` with `feedback.latest_sync_error()` in both the blocking (`stale-cache`/`missing`) and non-blocking (`refresh-failed-cache`) branches, so both existing Slack/Telegram messages (which already print `reason`) surface the real cause without any bot-file changes. Two new diagnostic/ops subcommands were added to `6_youtube_feedback.py`: `check-auth` (attempts only credential load/refresh, no API quota use) and `reauth` (forces a fresh interactive OAuth grant via `run_local_server`, usable headlessly on Lightsail through SSH local port forwarding). See `docs/usage/youtube-feedback.md` section 9 for the full re-auth runbook.

### 17. `over_target_length` demoted to warning (2026-08-02)

Status: monitor after deploy.

The 2026-08-01 retention-cliff commit (`7c30586`) cut `TARGET_DURATION_SEC` 80→55 (dev), which shrank the script length budget and hard cap (`MAX_SCRIPT_LENGTH_RATIO=1.40`) by the same 31%, but did not shrink what Stage 1 asks Stage 2 to fit into that budget (`required_beats` count, `evidence_status: limited` hedging language). Job `goal_20260802_025044_005235_f61bea61` produced a 549-char draft against a 358-char hard cap; the single Haiku compression pass (`revise_overlong_script`) only got it to 418 chars, and `validate_script()`'s `over_target_length` was still an `error` (deliberately promoted from `warning` on 2026-07-12, `290146091`), so `enforce_quality_without_revision()` raised `RuntimeError` and killed the whole job — no output was written at all.

Fix: `over_target_length` moved from `errors` to `warnings` in `validate_script()` (`0_script.py`), same production-continuity principle as `#14`. The real safety net is downstream: `stage_guard.py` measures actual TTS audio duration and tolerates 0.5x-1.8x of `TARGET_DURATION_SEC` (~27.5-99s at 55s), far more permissive than the 1.40x char-count proxy ever was. Per explicit product decision, no mechanical trimming was added — the compression pass still runs once, but if the result is still over the cap afterward, the job proceeds with that text as-is (natural narrative flow prioritized over hitting the length target exactly).

**Monitor**: check `script_quality.json` `over_target_length` frequency across the next batch of jobs. If it's frequent (≥30%, same bar as `#14`), the fix isn't the gate — it's that Stage 1's strategy prompt (`required_beats` count / hedging language) is oversized for the 55s budget and needs to be scaled down there instead.

Likely root cause of the original expiry: the Google Cloud OAuth consent screen is in "Testing" publishing status, which caps refresh tokens at 7 days — this must be verified/fixed in Google Cloud Console (Publish App → In production); the code cannot detect or change this. There is still no proactive/scheduled sync health-check (no cron/systemd timer) — sync only runs reactively when a plan/content command triggers it; this is a deliberate scope decision for now (see item 15's "scheduled loop" note).

### 18. story_type mix resets if `data/work/` is pruned (2026-08-02)

Status: accepted, monitor.

`story_types.recent_story_types()` reconstructs the recent-genre history by reading `story_type` out of `data/work/*/strategy.json`. That directory is gitignored working state, so anything that clears it (disk cleanup on Lightsail, a fresh container, moving to a new host) also erases the history the mix apportionment reasons over. The picker then cold-starts and hands out `principle_experience` first, and the observed distribution takes ~10 jobs to re-converge on the configured 35/30/25/10.

Partially mitigated: the feedback DB backfill (`content_features.format_type` → story_type, via `objective_planner._recent_format_types`) covers *published* videos even when the work directory is gone, so a channel with sync history degrades much less than a brand-new one. It does not cover jobs that were made but never published.

Not fixed on purpose: persisting story_type history to its own table would add a second source of truth for something the job artifacts already record, and the failure mode is a temporarily skewed genre mix — not a broken job. **Monitor**: if work-directory pruning becomes routine, check `story_type` distribution over the last 20 jobs before adding a dedicated store.

### 19. Script-stage guard deliberately not wired for story_type checks (2026-08-02)

Status: by design.

The story-types design spec offers "Stage 2 재시도 1회 또는 guard 실패" when a scene arrives without `visual.brief`. Neither was taken. A `stage_guard` check on the `script` stage feeds `pipeline_flow.run_stage`, which retries the stage once on guard failure — that retry re-runs Stage 1 + Stage 2 in full, doubling the most expensive Claude spend in the pipeline, which is exactly what this repo's cost convention forbids (see items 14/17 and `CLAUDE.md`).

Instead `normalize_story_scenes()` backfills a missing `brief` deterministically from the scene's own text, and `validate_script()` records `visual_brief_backfilled` / `role_off_sequence` / `role_bookends_missing` as warnings in `script_quality.json` at zero cost. **Monitor**: if `visual_brief_backfilled` shows up in ≥30% of jobs, the Stage 2 prompt is not landing the field and the prompt should be tightened — not the guard added.

### 20. X(Twitter) auto-posting assumptions are unverified against a live account (2026-08-02)

Status: new, verify against real credentials before relying on it.

`slack_bot._maybe_post_x_thread` now calls `x_poster.post_thread()` once a job reaches `stage == "done"`, so a Slack-driven job publishes to X with no second human step. Two additions on top of the existing posting path are unverified against a live account, because no X credentials existed when they were written:

- **Photo upload.** The lead tweet carries a rendered card (`x_photo_card.py`), uploaded via `POST https://api.x.com/2/media/upload` with the same OAuth 2.0 bearer `post_thread` already uses. Whether this account's tier and token scopes actually grant `media.write` is unconfirmed, as is the exact response shape — `_upload_media` accepts both `data.id` and `media_id_string` for that reason. Any failure is caught and the thread posts text-only.
- **`x_thread_adapter.LINK_COST_CHARS = 23`** — t.co's fixed link cost, used to decide how many source lines fit in the sources tweet. If X has changed the number, the sources tweet holds slightly too few or too many citations. Note the sources tweet is the one place where raw `len()` exceeds `TWEET_MAX_CHARS` by design: URLs collapse to 23 weighted chars on X, so the raw string is longer than the budget while the posted tweet is not.

Not blocking: every failure mode here degrades rather than breaks. No Pillow → no card (`x_photo_card` imports `card_render` lazily so the thread stage and both bots still import cleanly). Rejected image → text-only thread. Mid-thread failure → progress persisted, resumable with `/x_post`, and an already-posted thread refuses to repost.

**Action**: after the first real auto-post, confirm the lead tweet actually shows the card. If `media.write` turns out not to be granted, the log line is `사진 업로드 실패, 텍스트만 게시합니다` and the fix is a scope change in the X Developer Portal, not a code change.

### 21. Seed anchoring can thin out the autocomplete pool (2026-08-07)

Status: new, mitigated by a fallback; watch the counters on the first few live refreshes.

Risk-factor seeds are now probed anchored — `고혈압` becomes `치매 고혈압` (`dev/config/topic_domain.json`, `seed_anchors`) — so the suggestion pool is shaped toward brain health instead of returning straight cardiology. The cost is that a two-word query gets less autocomplete traffic than a one-word one, and `trend_probe.probe()` drops any language rung that returns fewer than `MIN_KEPT = 3` on-domain suggestions. If every rung fails for every anchored seed, the eligible queue thins out or empties.

Mitigation already in place: `batch_expand()` retries an `off_topic` anchored seed once with its bare seed, and the resulting off-domain candidates are caught by the `no_domain_anchor` rejection instead of reaching the queue. So the failure mode is wasted suggest calls, not a wrong topic.

**Action**: after a live `--refresh`, read `data/topics/raw/{stamp}_run.json`:

- `anchored_seed_count` — how many seeds carried an anchor
- `anchor_fallback_count` — how many of those had to fall back

If the fallback count exceeds roughly half the anchored count, the anchors are too rare a phrasing. Change `seed_anchors` in `topic_domain.json` to something people actually type, or raise `max_anchor_variants` to 2 so a second anchor gets a turn (note: that also raises the suggest call count, bounded by `max_seeds_total`).

### 22. `threshold` is calibrated against the current weights, not independent of them (2026-08-07)

Status: new, informational.

`topic_score`'s weights were rebalanced to make room for `domain_relevance` (30), and `threshold` was lowered 60 → 45 to match. The two numbers are not independently meaningful: with the anchor gate in place, the *minimum possible* score for an in-domain candidate is 40 (`domain_relevance` 20 + `novelty` 15 + `safety_tone` 5), so 45 asks for roughly one further signal beyond the anchor. Measured separation at the time of the change (novelty penalty applied): in-domain samples 48–78, off-domain samples all gated out before scoring.

**Action**: if any weight in `dev/config/topic_score_rules.json` changes, re-derive `threshold` rather than carrying 45 over. `topic_candidate_pipeline.py --score-text "..."` prints a full breakdown with no network call, and `tests/test_topic_score.py::ShippedConfigCalibrationTests` asserts the separation against the committed config — it will fail if a weight edit breaks it.

## Bugs To Watch For In Next Testing Session

- Duplicate welcome/bye messages during rapid systemd restart.
- Missing bye message if process is killed with SIGKILL or server shuts down hard.
- Old Telegram/Slack buttons after service restart should still be rejected if stage mismatch.
- If busy flag is left stuck after a hard process kill, `/status` may show `busy`; `/cancel` clears the job.
- Progress messages for very short render jobs may only show start and complete.
- Caption sync should be reviewed on real rendered output after the Korean grammar segmentation rewrite (`213f3f1`); tune `CAPTION_OFFSET_SEC` if needed.
- 3.6–8 second retention drop-off on newly published Shorts (see item 14) — target ≤10 percentage points on the first 5 videos made with the new prompt; if 10–20pp, tighten the Scene 2 rule further; if >20pp, the hypothesis is wrong and the cause is probably audio/caption density, not script structure.
