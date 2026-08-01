# Known Issues and Risk Register

Last updated: 2026-08-01
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

Likely root cause of the original expiry: the Google Cloud OAuth consent screen is in "Testing" publishing status, which caps refresh tokens at 7 days — this must be verified/fixed in Google Cloud Console (Publish App → In production); the code cannot detect or change this. There is still no proactive/scheduled sync health-check (no cron/systemd timer) — sync only runs reactively when a plan/content command triggers it; this is a deliberate scope decision for now (see item 15's "scheduled loop" note).

## Bugs To Watch For In Next Testing Session

- Duplicate welcome/bye messages during rapid systemd restart.
- Missing bye message if process is killed with SIGKILL or server shuts down hard.
- Old Telegram/Slack buttons after service restart should still be rejected if stage mismatch.
- If busy flag is left stuck after a hard process kill, `/status` may show `busy`; `/cancel` clears the job.
- Progress messages for very short render jobs may only show start and complete.
- Caption sync should be reviewed on real rendered output after the Korean grammar segmentation rewrite (`213f3f1`); tune `CAPTION_OFFSET_SEC` if needed.
- 3.6–8 second retention drop-off on newly published Shorts (see item 14) — target ≤10 percentage points on the first 5 videos made with the new prompt; if 10–20pp, tighten the Scene 2 rule further; if >20pp, the hypothesis is wrong and the cause is probably audio/caption density, not script structure.
