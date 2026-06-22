# Environment Capture

Last updated: 2026-06-22

## Local Codex Desktop Environment

This capture comes from the local Codex desktop thread, not the AWS Lightsail server.

Workspace/repo used during most recent work:

```text
C:\Users\stlsh\AppData\Local\Temp\short_pipeline_work\repo
```

Original writable workspace root reported by Codex:

```text
C:\Users\stlsh\Documents\short_pipeline
```

Branch:

```text
codex/lightsail-stability
```

Python executable used for local validation:

```text
C:\Users\stlsh\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe
```

Git executable available in Codex runtime:

```text
C:\Users\stlsh\.cache\codex-runtimes\codex-primary-runtime\dependencies\native\git\cmd\git.exe
```

Node executable available in Codex runtime:

```text
C:\Users\stlsh\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe
```

Local Windows environment did not have usable WSL/bash during the last checks, so shell syntax validation with `bash -n` could not be completed locally.

## Local pip freeze

Captured with:

```bash
python -m pip freeze
```

Output:

```text
annotated-types==0.7.0
artifact_tool_v2 @ file:///D:/a/openai/openai/lib/agent/tools/artifact_tool_v2
cffi==2.0.0
charset-normalizer==3.4.7
cryptography==49.0.0
et_xmlfile==2.0.0
lxml==6.0.2
numpy==2.3.5
openpyxl==3.1.5
packaging==26.2
pandas==3.0.1
pdf2image==1.17.0
pdfminer.six==20251230
pdfplumber==0.11.9
pillow==12.2.0
pycparser==3.0
pydantic==2.13.4
pydantic_core==2.46.4
pyhumps==3.8.0
pypdf==6.10.0
pypdfium2==5.9.0
python-dateutil==2.9.0.post0
python-docx==1.2.0
python-pptx==1.0.2
reportlab==4.4.9
setuptools==82.0.1
six==1.17.0
typing-inspection==0.4.2
typing_extensions==4.15.0
tzdata==2026.2
wheel==0.47.0
xlsxwriter==3.2.9
```

## Lightsail Runtime Facts Observed From User Logs

Server prompt shown by user:

```text
ubuntu@ip-172-26-0-164:~/brain50$
```

Expected project path:

```text
/home/ubuntu/brain50
```

Observed Python version in traceback:

```text
/usr/lib/python3.10
```

Observed TTS binary:

```text
/home/ubuntu/.local/bin/supertonic
```

Commands user ran successfully:

```bash
which supertonic
ls ~/.local/bin/supertonic
```

Both returned:

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

Optional:

```bash
export TELEGRAM_POLL_ERROR_NOTIFY_INTERVAL=1800
export TTS_VOICE=M2
```

## Expected System Packages / Binaries on Lightsail

```bash
python3
pip3
ffmpeg
ffprobe
supertonic
git
```

For YouTube upload, the existing project may require Google/YouTube credentials configured outside the committed repo. Inspect `src/4_upload.py` and any untracked server-side credential files before changing upload behavior.

## Useful Validation Commands

On a Linux/Cloud environment:

```bash
python3 -m py_compile dev/src/*.py prod/src/*.py
bash -n dev/sh/*.sh prod/sh/*.sh deploy/lightsail/*.sh
git diff --check
```

On the previous Windows Codex environment, use explicit file expansion for Python:

```powershell
$files = git ls-files 'dev/src/*.py' 'prod/src/*.py'
python -m py_compile $files
```