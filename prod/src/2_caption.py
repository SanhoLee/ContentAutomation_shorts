from faster_whisper import WhisperModel
import os

WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))

with open(os.path.join(WORK_DIR, "script.txt"), "r", encoding="utf-8") as f:
    script_text = f.read().strip()

model = WhisperModel("small", device="cpu", compute_type="int8")
segments, info = model.transcribe(
    os.path.join(WORK_DIR, "voice.wav"),
    word_timestamps=True,
    language="ko",
    initial_prompt=script_text
)

def fmt(t):
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int((t - int(t)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

words = []
for seg in segments:
    for w in seg.words:
        words.append(w)

# 이 조사/어미 뒤에서 줄바꿈을 우선적으로 시도
BREAK_SUFFIXES = [
    "습니다", "입니다", "이에요", "거든요", "네요",
    "지만", "는데", "으로", "에서", "에게", "까지", "부터", "처럼", "보다",
    "이나", "라서", "면서",
    "은", "는", "이", "가", "을", "를", "에", "로", "와", "과", "도", "만",
    "다", "요", "죠",
]

MAX_CHARS = 16  # 한 줄 최대 글자수
MIN_CHARS = 6   # 최소 글자수 (너무 짧게 끊기지 않도록)

def clean(w):
    return w.word.strip()

def ends_sentence(w):
    return any(p in clean(w) for p in [".", "!", "?"])

def ends_with_break(w):
    text = clean(w).rstrip(".,!?")
    return any(text.endswith(s) for s in BREAK_SUFFIXES)

lines = []
current = []
length = 0

for w in words:
    current.append(w)
    length += len(clean(w))

    if ends_sentence(w):
        lines.append(current); current = []; length = 0
    elif length >= MAX_CHARS:
        lines.append(current); current = []; length = 0
    elif length >= MIN_CHARS and ends_with_break(w):
        lines.append(current); current = []; length = 0

if current:
    lines.append(current)

with open(os.path.join(WORK_DIR, "subs.srt"), "w", encoding="utf-8") as f:
    idx = 1
    for chunk in lines:
        start = chunk[0].start
        end = chunk[-1].end
        text = " ".join(clean(w) for w in chunk)
        f.write(f"{idx}\n{fmt(start)} --> {fmt(end)}\n{text}\n\n")
        idx += 1

print("자막 생성 완료 (한국어 끊기 적용)")

# ===== 장면별 타이밍 계산 =====
import json
import re

with open(os.path.join(WORK_DIR, "scenes.json"), "r", encoding="utf-8") as f:
    scenes = json.load(f)

def korean_chars(text):
    return len(re.sub(r'[^\uAC00-\uD7A3]', '', text))

word_idx = 0
for scene in scenes:
    scene_char_len = korean_chars(scene["text"])
    accumulated = 0
    start_idx = word_idx

    while word_idx < len(words) and accumulated < scene_char_len:
        accumulated += korean_chars(clean(words[word_idx]))
        word_idx += 1

    # 안전장치: word_idx가 시작 지점에서 전혀 못 움직인 경우 최소 1단어는 포함
    if word_idx == start_idx and word_idx < len(words):
        word_idx += 1

    end_idx = min(word_idx, len(words)) - 1
    end_idx = max(end_idx, start_idx)

    scene["start"] = words[start_idx].start
    scene["end"] = words[end_idx].end
    scene["duration"] = round(scene["end"] - scene["start"], 2)

# 마지막 장면의 end를 음성 전체 끝까지 보정 (남는 word 포함)
if scenes:
    scenes[-1]["end"] = words[-1].end
    scenes[-1]["duration"] = round(scenes[-1]["end"] - scenes[-1]["start"], 2)

# 화면 전환 타이밍: 공백 구간을 포함해서 다음 장면 시작 직전까지 채움
voice_total = words[-1].end  # voice.wav 전체 길이 근사

for i, scene in enumerate(scenes):
    render_start = scenes[i - 1]["render_end"] if i > 0 else 0.0
    if i < len(scenes) - 1:
        render_end = scenes[i + 1]["start"]
    else:
        render_end = voice_total

    scene["render_start"] = round(render_start, 2)
    scene["render_end"] = round(render_end, 2)
    scene["render_duration"] = round(render_end - render_start, 2)

with open(os.path.join(WORK_DIR, "scenes_timed.json"), "w", encoding="utf-8") as f:
    json.dump(scenes, f, ensure_ascii=False, indent=2)

print("\n=== 장면별 타이밍 ===")
for i, s in enumerate(scenes):
    print(f"{i}: {s['start']:.2f}s ~ {s['end']:.2f}s ({s['duration']:.2f}s) - {s['visual_query']}")