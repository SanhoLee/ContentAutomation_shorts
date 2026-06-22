# Known Issues and Risk Register

Last updated: 2026-06-22
Branch: `codex/lightsail-stability`

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

Risk:

- If Telegram API is unreachable for a long period, bot command handling is delayed.

Recommended follow-up:

- Consider external uptime monitoring if the user wants off-server alerts when the whole server/bot is down.

### 2. Background thread state persistence

Status: improved.

The Telegram bot runs long tasks in background threads and also polls messages in the main loop. This created a risk that `data/telegram_state.json` could be written concurrently.

Current handling:

- `STATE_LOCK` protects state writes.
- State is written to a temporary file and atomically replaced via `os.replace`.

Remaining risk:

- The in-memory `state` dict is still shared between main thread and background task. Current usage is simple, but a more robust future design could use a queue or a single state manager.

### 3. `/approve` text command has no stage token

Status: acceptable but less strict than inline buttons.

Inline buttons now carry stage tokens and reject stale buttons. Text command `/approve` still approves whatever current stage is in `job["stage"]`.

Reason:

- `/approve` is an explicit current-stage command.
- Stale button accidents were the main issue.

Future option:

- Remove `/approve` or make it require `/approve await_caption_approval` if stricter behavior is desired.

### 4. Render progress is based on ffmpeg progress file

Status: implemented.

`2_render.sh` writes `render_progress.txt` via ffmpeg `-progress`. Telegram reads it and sends checkpoints: start, 25%, 50%, 75%, complete.

Risks:

- Very short renders may skip intermediate checkpoints before completion.
- If `ffmpeg` changes progress field format, ratio parsing may need adjustment.
- Shell syntax could not be validated locally because WSL/bash was unavailable in the Windows Codex environment. Python validation and `git diff --check` passed.

### 5. TTS CLI path under systemd

Status: mitigated.

Observed error:

```text
FileNotFoundError: [Errno 2] No such file or directory: 'supertonic'
```

Cause:

- `supertonic` existed at `/home/ubuntu/.local/bin/supertonic`, but systemd PATH did not include that directory.

Current handling:

- `config.sh` prepends `$HOME/.local/bin:/usr/local/bin` to PATH.
- `1_tts.py` checks `TTS_BIN`, `SUPERTONIC_BIN`, PATH, and common bin directories.
- Clear error message is shown if the executable is still missing.

Recommended server setting:

```bash
export TTS_BIN=/home/ubuntu/.local/bin/supertonic
```

Put it in `dev/secrets.sh` or `prod/secrets.sh`.

### 6. PubMed no-result topics

Status: handled.

If PubMed returns no direct hits, generation continues. Claude is instructed not to fabricate paper-specific numbers/results and to use cautious general medical information.

Risk:

- Content may be less evidence-specific.

Recommended user flow:

- Telegram shows PubMed status and `pubmed_status.json`.
- User may `/retry new topic` if topic is too broad, too specific, or consumer-search oriented.

### 7. Claude API read timeout

Status: mitigated.

Original issue:

- Claude response could exceed 20 seconds and fail because general `REQUEST_TIMEOUT` was used.

Current handling:

- `CLAUDE_TIMEOUT`, default 180 seconds.
- `CLAUDE_RETRIES`, default 2.
- 429/5xx/timeouts retry.

Recommended server tuning:

```bash
export CLAUDE_TIMEOUT=300
```

### 8. Telegram file editing UX limitations

Status: pragmatic workaround.

Telegram does not let a bot make its sent message directly editable by the user as a file editor. Current workflow:

1. Bot sends original file.
2. User edits file locally/mobile-supported editor or sends full replacement text.
3. User uploads edited file or sends text message.
4. Bot overwrites the relevant artifact.

Editable artifacts:

- `script.txt`
- `subs.srt`
- `video_meta.json`

### 9. YouTube upload final state

Status: not heavily exercised in recent local testing.

Upload is only expected after final metadata approval. It should upload as private according to existing upload script behavior. Cloud thread should inspect `src/4_upload.py` before modifying upload behavior.

### 10. Encoding in Windows terminal output

Status: local display issue.

Some Korean text appears mojibake in PowerShell command output, but files are UTF-8. Use `PYTHONIOENCODING=utf-8` or read files in an editor before assuming source text is corrupt.

## Bugs to Watch For in Next Testing Session

- Duplicate welcome/bye messages during rapid systemd restart.
- Missing bye message if process is killed with SIGKILL or server shuts down hard.
- Old Telegram buttons after service restart should still be rejected if stage mismatch.
- If busy flag is left stuck after a hard process kill, `/status` may show `busy`. Restart normally should load state; if stuck, `/cancel` clears the job.
- Progress messages for very short render jobs may only show start and complete.