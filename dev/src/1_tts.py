import os
import shutil
import subprocess

WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
TTS_BIN = os.environ.get("TTS_BIN") or os.environ.get("SUPERTONIC_BIN") or "supertonic"


def resolve_executable(command):
    expanded = os.path.expanduser(command)
    if os.path.isabs(expanded) or os.sep in expanded:
        return expanded if os.path.exists(expanded) else None
    found = shutil.which(expanded)
    if found:
        return found
    for directory in ("~/.local/bin", "/usr/local/bin", "/usr/bin", "/bin"):
        candidate = os.path.expanduser(os.path.join(directory, expanded))
        if os.path.exists(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


with open(os.path.join(WORK_DIR, "script.txt"), "r", encoding="utf-8") as f:
    TEXT = f.read().strip()

raw_path = os.path.join(WORK_DIR, "voice_raw.wav")
final_path = os.path.join(WORK_DIR, "voice.wav")

tts_executable = resolve_executable(TTS_BIN)
if not tts_executable:
    raise SystemExit(
        "TTS 실행 파일을 찾지 못했습니다. "
        "Lightsail에서 `which supertonic` 또는 `ls ~/.local/bin/supertonic`으로 설치 여부를 확인하세요. "
        "설치 위치가 다르면 secrets.sh 또는 systemd 환경에 `TTS_BIN=/절대/경로/supertonic`을 설정하세요. "
        f"현재 PATH={os.environ.get('PATH', '')}"
    )

subprocess.run([
    tts_executable, "tts", TEXT,
    "-o", raw_path,
    "--lang", "ko",
    "--voice", os.environ.get("TTS_VOICE", "F1")
], check=True)

# 속도 후처리 (피치 유지, atempo)
ATEMPO = float(os.environ.get("ATEMPO", "1.0"))

if ATEMPO != 1.0:
    ffmpeg_bin = resolve_executable(os.environ.get("FFMPEG_BIN", "ffmpeg"))
    if not ffmpeg_bin:
        raise SystemExit("ffmpeg 실행 파일을 찾지 못했습니다. `sudo apt install ffmpeg` 후 다시 실행하세요.")
    subprocess.run([
        ffmpeg_bin, "-y", "-i", raw_path,
        "-filter:a", f"atempo={ATEMPO}",
        final_path
    ], check=True)
    os.remove(raw_path)
else:
    os.replace(raw_path, final_path)
