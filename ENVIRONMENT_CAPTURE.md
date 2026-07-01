# Environment Capture

Last updated: 2026-07-02

## Codex Workspace Notes

This capture is for Codex-hosted work, not the AWS Lightsail server itself.

The practical working checkout used for recent PRs:

```text
C:\Users\stlsh\AppData\Local\Temp\short_pipeline_work\repo
```

The originally reported writable workspace root may be mostly empty except `.git`/agent metadata:

```text
C:\Users\stlsh\Documents\short_pipeline
```

Current source of truth branch:

```text
main
```

Use feature branches from `origin/main`, for example:

```bash
git fetch origin main
git switch -c codex/some-fix origin/main
```

## Recent Local Tooling Facts

- Windows PowerShell is the local shell.
- GitHub CLI may be installed but auth can be expired; use the GitHub app connector for PR creation when possible.
- Local Python may not have all production dependencies such as `faster_whisper`.
- For Korean output in Windows console tests, set `PYTHONIOENCODING=utf-8`.
- WSL/bash may not be available locally; run `bash -n` in Cloud/Linux when shell scripts are touched.

## Lightsail Runtime Facts Observed From User Logs

Server prompt shown by user:

```text
ubuntu@ip-172-26-0-164:~/brain50$
```

Expected project path:

```text
/home/ubuntu/brain50
```

Observed Python version in tracebacks:

```text
/usr/lib/python3.10
```

Observed TTS binary:

```text
/home/ubuntu/.local/bin/supertonic
```

## Recommended Lightsail Environment Variables

In `dev/secrets.sh` and/or `prod/secrets.sh`:

```bash
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."
export ANTHROPIC_API_KEY="..."
export PEXELS_API_KEY="..."
export TTS_BIN=/home/ubuntu/.local/bin/supertonic
export CLAUDE_TIMEOUT=300
```

Current optional/tuning variables:

```bash
export TELEGRAM_POLL_ERROR_NOTIFY_INTERVAL=1800
export TTS_VOICE=M2
export ENABLE_WEB_RESEARCH=true
export WEB_RESEARCH_TIMEOUT=60
export WEB_RESEARCH_MAX_USES=3
export WEB_RESEARCH_MAX_TOKENS=900
export WEB_RESEARCH_MAX_TOOL_TURNS=2
export CAPTION_OFFSET_SEC=-0.15
```

## Expected System Packages / Binaries On Lightsail

```bash
python3
pip3
ffmpeg
ffprobe
supertonic
git
```

The caption stage requires faster-whisper and its runtime dependencies. YouTube upload may require Google/YouTube credential files outside the committed repo.

## Useful Validation Commands

On Linux/Cloud:

```bash
python3 -m compileall -q dev/src
python3 -m compileall -q prod/src
bash -n dev/sh/*.sh prod/sh/*.sh deploy/lightsail/*.sh
git diff --check
```

On Windows Codex, prefer explicit paths:

```powershell
python -m py_compile dev\src\0_script.py dev\src\telegram_bot.py
python -m py_compile dev\src\2_caption.py
git diff --check
```

For tests that import modules requiring unavailable production dependencies, use narrow mocks only for pure logic tests, and clearly report that full runtime execution was not performed.
