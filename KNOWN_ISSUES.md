# Known Issues and Risk Register

Last updated: 2026-07-02
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

Status: recently refactored.

Recent failures included missing `total_chars` and `ENABLE_WEB_RESEARCH`. The fix is `dev/src/script_runtime.py`, which centralizes Stage 0 env defaults and derived values. Avoid reintroducing new global env parsing directly in `dev/src/0_script.py`; add new runtime knobs to `script_runtime.py` instead.

### 5. web_search cost and timeout behavior

Status: bounded.

Current defaults:

```bash
ENABLE_WEB_RESEARCH=true
WEB_RESEARCH_TIMEOUT=60
WEB_RESEARCH_MAX_USES=3
WEB_RESEARCH_MAX_TOKENS=900
WEB_RESEARCH_MAX_TOOL_TURNS=2
```

web_search is optional. Timeout/tool errors return an empty supplement and script generation continues. It should not retry automatically after timeout because the request may already be processing server-side, creating duplicate cost risk.

### 6. Caption timing may still need empirical tuning

Status: improved, watch in rendered output.

`dev/src/2_caption.py` now uses sequential Whisper word timestamp consumption and `CAPTION_OFFSET_SEC=-0.15`. If captions still lag, try a slightly more negative offset such as `-0.20`. If captions appear early, move toward `0`. This is a perceptual tuning knob, not a render margin/font setting.

### 7. Render progress is based on ffmpeg progress file

Status: implemented.

`2_render.sh` writes `render_progress.txt` via ffmpeg `-progress`. Telegram reads it and sends checkpoints: start, 25%, 50%, 75%, complete. Very short renders may skip intermediate checkpoints.

### 8. TTS CLI path under systemd

Status: mitigated.

`config.sh` prepends `$HOME/.local/bin:/usr/local/bin` to PATH. `1_tts.py` checks `TTS_BIN`, `SUPERTONIC_BIN`, PATH, and common bin directories.

Recommended server setting:

```bash
export TTS_BIN=/home/ubuntu/.local/bin/supertonic
```

### 9. PubMed no-result topics

Status: handled.

If PubMed returns no direct hits, generation continues. Claude is instructed not to fabricate paper-specific numbers/results and to use cautious general medical information. Telegram can show `pubmed_status.json`; user may `/retry new topic`.

### 10. Claude API read timeout

Status: mitigated.

- `CLAUDE_TIMEOUT`, default 180 seconds; server tuning often uses 300.
- HTTP 429/5xx may retry inside `CLAUDE_HTTP_RETRIES`.
- Read/connect timeout is not automatically retried to reduce duplicate cost risk.

### 11. Telegram file editing UX limitations

Status: pragmatic workaround.

Editable artifacts:

- `script.txt`
- `subs.srt`
- `video_meta.json`

The bot sends files; the user uploads replacement text/file to overwrite the relevant artifact.

### 12. YouTube upload final state

Status: not heavily exercised in recent local testing.

Upload is expected after final metadata approval. Inspect `src/4_upload.py` before changing upload behavior.

### 13. Encoding in Windows terminal output

Status: local display issue.

Some Korean text can appear mojibake or fail to print under Windows console encodings. Use `PYTHONIOENCODING=utf-8` or inspect files directly before assuming source corruption.

## Bugs To Watch For In Next Testing Session

- Duplicate welcome/bye messages during rapid systemd restart.
- Missing bye message if process is killed with SIGKILL or server shuts down hard.
- Old Telegram buttons after service restart should still be rejected if stage mismatch.
- If busy flag is left stuck after a hard process kill, `/status` may show `busy`; `/cancel` clears the job.
- Progress messages for very short render jobs may only show start and complete.
- Caption sync should be reviewed on real rendered output after PR #38; tune `CAPTION_OFFSET_SEC` if needed.
