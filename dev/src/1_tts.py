import subprocess
import os

WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))

with open(os.path.join(WORK_DIR, "script.txt"), "r", encoding="utf-8") as f:
    TEXT = f.read().strip()

raw_path = os.path.join(WORK_DIR, "voice_raw.wav")
final_path = os.path.join(WORK_DIR, "voice.wav")

subprocess.run([
    "supertonic", "tts", TEXT,
    "-o", raw_path,
    "--lang", "ko",
    "--voice", "M4"
], check=True)

# 속도 후처리 (피치 유지, atempo)
ATEMPO = float(os.environ.get("ATEMPO", "1.0"))

if ATEMPO != 1.0:
    subprocess.run([
        "ffmpeg", "-y", "-i", raw_path,
        "-filter:a", f"atempo={ATEMPO}",
        final_path
    ], check=True)
    os.remove(raw_path)
else:
    os.replace(raw_path, final_path)
