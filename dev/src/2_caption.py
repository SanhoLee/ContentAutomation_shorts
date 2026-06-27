"""
2_caption.py — 자막 생성 (caption_script.txt 기반 Method 2)

핵심 원칙:
  자막 텍스트 = caption_script.txt (원천 — 영어 약어 영어 유지, 기호만 변환)
  타임스탬프  = faster-whisper (tts_script.txt를 initial_prompt로 주입)

기존 방식 문제:
  whisper STT 결과 텍스트를 자막으로 사용 → 조사/어미 끊김 고질 문제

개선된 흐름:
  ① caption_script.txt → split_script_to_lines()
     한국어 문법 기반 라인 분할 (조사·어미 앞에서 절대 끊지 않음)
  ② voice.wav + initial_prompt=tts_script.txt → get_whisper_words()
     TTS가 실제로 발화한 텍스트로 힌트 → 타임스탬프 정확도 향상
  ③ align_lines_to_timestamps()
     음절수 비율 기반 정렬 → 단어 경계 스냅
  → subs.srt / scenes_timed.json
"""

import json
import os
import re

from faster_whisper import WhisperModel

WORK_DIR   = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
MAX_CHARS  = int(os.environ.get("CAPTION_MAX_CHARS", "16"))
MIN_CHARS  = int(os.environ.get("CAPTION_MIN_CHARS", "6"))
MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")


# ─────────────────────────────────────────────
# 한국어 패턴 정의
# ─────────────────────────────────────────────

# 문장 끝 → 즉시 줄바꿈
_SENTENCE_END = re.compile(
    r"(습니다|입니다|이에요|아요|어요|거든요|네요|고요|ㄴ데요|군요|잖아요"
    r"|죠|요)[.!?]?$"
)

# 절 경계 → 충분히 길 때 줄바꿈
_CLAUSE_BREAK = re.compile(
    r"(지만|는데|으로|에서|에게|까지|부터|처럼|보다|이나|라서|면서"
    r"|그리고|그래서|그런데|하지만|또한|또|고)$"
)

# 조사 → 앞 토큰에 반드시 붙임 (이 앞에서 줄바꿈 금지)
_ATTACH = re.compile(
    r"^(을|를|이|가|은|는|의|에|로|으로|와|과|도|만"
    r"|에서|부터|까지|에게|처럼|보다|이나|라도"
    r"|이다|이에요|이야|입니다)$"
)


# ─────────────────────────────────────────────
# 음절 수 계산 (타임스탬프 비율 기준)
# ─────────────────────────────────────────────

def _syllables(text: str) -> float:
    """
    한국어 실제 발화 시간은 '음절 수'에 비례.
    한글 1자 = 1음절, 숫자 1자 ≈ 1음절, 영어 1자 ≈ 0.4음절(짧게)
    """
    ko  = len(re.findall(r"[\uAC00-\uD7A3]", text))
    num = len(re.findall(r"\d", text))
    eng = len(re.findall(r"[A-Za-z]", text))
    return ko + num + eng * 0.4


# ─────────────────────────────────────────────
# 1. 자막 라인 분할
# ─────────────────────────────────────────────

def split_script_to_lines(script_text: str) -> list[str]:
    """
    caption_script.txt를 한국어 문법 기반으로 자막 라인 분할.
    조사·어미가 앞 단어와 분리되지 않도록 처리.
    """
    tokens  = script_text.replace("\n", " ").replace("\r", " ").split()
    tokens  = [t for t in tokens if t]

    lines   = []
    current = []
    cur_len = 0

    for i, token in enumerate(tokens):
        tok_len = _syllables(token)

        # 조사/어미 → 현재 라인에 무조건 붙임
        if _ATTACH.match(token) and current:
            current.append(token)
            cur_len += tok_len
            if _SENTENCE_END.search("".join(current)):
                lines.append("".join(current))
                current = []; cur_len = 0
            continue

        # 최대 글자수 초과 → 현재 라인 마감
        if cur_len + tok_len > MAX_CHARS and cur_len >= MIN_CHARS:
            lines.append("".join(current))
            current = []; cur_len = 0

        current.append(token)
        cur_len += tok_len
        joined   = "".join(current)

        # 다음 토큰이 조사면 여기서 끊지 않음
        next_tok = tokens[i + 1] if i + 1 < len(tokens) else ""
        if _ATTACH.match(next_tok):
            continue

        if _SENTENCE_END.search(joined):
            lines.append(joined)
            current = []; cur_len = 0
        elif _CLAUSE_BREAK.search(joined) and cur_len >= MIN_CHARS:
            lines.append(joined)
            current = []; cur_len = 0

    if current:
        lines.append("".join(current))

    return [l for l in lines if l.strip()]


# ─────────────────────────────────────────────
# 2. Whisper → 단어 타임스탬프
# ─────────────────────────────────────────────

def get_whisper_words(audio_path: str, tts_script: str) -> list[dict]:
    """
    faster-whisper로 단어 타임스탬프만 추출.
    initial_prompt = tts_script (TTS가 실제로 발화한 텍스트)
    → whisper가 발화 텍스트에 가깝게 인식해 타임스탬프 정확도 향상.
    자막 텍스트는 이 결과를 사용하지 않음.
    """
    print(f"🎙️  Whisper 타임스탬프 추출 (model={MODEL_SIZE})...")
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")

    # initial_prompt: tts_script 앞 500자 (TTS가 실제 발화한 텍스트 기준)
    hint = tts_script[:500]

    segments, _ = model.transcribe(
        audio_path,
        language="ko",
        word_timestamps=True,
        initial_prompt=hint,
        beam_size=5,
        vad_filter=True,
        vad_parameters={
            "min_silence_duration_ms": 150,  # 빠른 발화 대응 (기존 300 → 150)
            "speech_pad_ms": 100,
        },
    )

    words = []
    for seg in segments:
        if seg.words:
            for w in seg.words:
                words.append({
                    "word":  w.word.strip(),
                    "start": w.start,
                    "end":   w.end,
                })

    if words:
        print(f"  단어 {len(words)}개, 총 {words[-1]['end']:.1f}s")
    else:
        print("  ⚠️  단어 타임스탬프 없음")
    return words


# ─────────────────────────────────────────────
# 3. 라인 ↔ 타임스탬프 매핑
# ─────────────────────────────────────────────

def _snap(target: float, words: list[dict], mode: str) -> float:
    """target_time에 가장 가까운 단어 경계(start/end)로 스냅."""
    if not words:
        return target
    key  = "start" if mode == "start" else "end"
    best = min(words, key=lambda w: abs(w[key] - target))
    return best[key]


def align_lines_to_timestamps(lines: list[str], words: list[dict]) -> list[dict]:
    """
    음절수 비율 기반으로 각 캡션 라인의 start/end 타임스탬프 추정 후
    가장 가까운 단어 경계에 스냅.
    """
    if not words:
        print("⚠️  단어 없음 — 균등 분할 fallback")
        t = 0.0
        result = []
        for line in lines:
            result.append({"text": line, "start": t, "end": t + 2.0})
            t += 2.0
        return result

    audio_start  = words[0]["start"]
    audio_end    = words[-1]["end"]
    total_dur    = audio_end - audio_start
    total_syl    = sum(_syllables(l) for l in lines) or 1.0

    result  = []
    syl_cur = 0.0

    for line in lines:
        syl_len = _syllables(line)

        raw_s = audio_start + (syl_cur / total_syl) * total_dur
        raw_e = audio_start + ((syl_cur + syl_len) / total_syl) * total_dur

        snapped_s = _snap(raw_s, words, "start")
        snapped_e = _snap(raw_e, words, "end")

        # 최소 0.3초 보장
        if snapped_e - snapped_s < 0.3:
            snapped_e = min(snapped_s + 0.3, audio_end)

        # 이전 라인과 역전 방지
        if result and snapped_s < result[-1]["end"]:
            snapped_s = result[-1]["end"]
            snapped_e = max(snapped_e, snapped_s + 0.3)

        result.append({
            "text":  line,
            "start": round(snapped_s, 3),
            "end":   round(snapped_e, 3),
        })
        syl_cur += syl_len

    if result:
        result[-1]["end"] = round(audio_end, 3)

    return result


# ─────────────────────────────────────────────
# 4. SRT 출력
# ─────────────────────────────────────────────

def _fmt(t: float) -> str:
    h  = int(t // 3600)
    m  = int((t % 3600) // 60)
    s  = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(captions: list[dict], path: str):
    with open(path, "w", encoding="utf-8") as f:
        for i, cap in enumerate(captions, 1):
            f.write(f"{i}\n{_fmt(cap['start'])} --> {_fmt(cap['end'])}\n{cap['text']}\n\n")
    print(f"✅ subs.srt ({len(captions)}개 라인) → {path}")


# ─────────────────────────────────────────────
# 5. 장면별 타이밍 (기존 로직 유지)
# ─────────────────────────────────────────────

def calc_scene_timing(scenes: list[dict], words: list[dict]) -> list[dict]:
    if not words:
        return scenes

    def syl(text):
        return _syllables(text)

    word_idx = 0
    for scene in scenes:
        sc_syl  = syl(scene["text"])
        acc     = 0.0
        st_idx  = word_idx

        while word_idx < len(words) and acc < sc_syl:
            acc      += syl(words[word_idx]["word"])
            word_idx += 1

        if word_idx == st_idx and word_idx < len(words):
            word_idx += 1

        end_idx = max(min(word_idx, len(words)) - 1, st_idx)
        scene["start"]    = words[st_idx]["start"]
        scene["end"]      = words[end_idx]["end"]
        scene["duration"] = round(scene["end"] - scene["start"], 2)

    if scenes:
        scenes[-1]["end"]      = words[-1]["end"]
        scenes[-1]["duration"] = round(scenes[-1]["end"] - scenes[-1]["start"], 2)

    voice_total = words[-1]["end"]
    for i, scene in enumerate(scenes):
        rs = scenes[i - 1]["render_end"] if i > 0 else 0.0
        re_ = scenes[i + 1]["start"] if i < len(scenes) - 1 else voice_total
        scene["render_start"]    = round(rs,  2)
        scene["render_end"]      = round(re_, 2)
        scene["render_duration"] = round(re_ - rs, 2)

    return scenes


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────

def main():
    caption_script_path = os.path.join(WORK_DIR, "caption_script.txt")
    tts_script_path     = os.path.join(WORK_DIR, "tts_script.txt")
    script_path         = os.path.join(WORK_DIR, "script.txt")   # fallback
    audio_path          = os.path.join(WORK_DIR, "voice.wav")
    scenes_path         = os.path.join(WORK_DIR, "scenes.json")
    srt_path            = os.path.join(WORK_DIR, "subs.srt")
    timed_path          = os.path.join(WORK_DIR, "scenes_timed.json")

    # ── caption_script.txt 읽기 (없으면 script.txt fallback)
    if os.path.exists(caption_script_path):
        with open(caption_script_path, "r", encoding="utf-8") as f:
            caption_text = f.read().strip()
        print(f"📄 caption_script.txt 사용 ({len(caption_text)}자)")
    else:
        print("⚠️  caption_script.txt 없음 — script.txt fallback (1_tts.py를 먼저 실행하세요)")
        with open(script_path, "r", encoding="utf-8") as f:
            caption_text = f.read().strip()

    # ── tts_script.txt 읽기 (initial_prompt용)
    if os.path.exists(tts_script_path):
        with open(tts_script_path, "r", encoding="utf-8") as f:
            tts_text = f.read().strip()
    else:
        tts_text = caption_text  # fallback

    # ── Step 1: 자막 라인 분할 (caption 텍스트 기준)
    print("✂️  자막 라인 분할 (한국어 문법 기반)...")
    lines = split_script_to_lines(caption_text)
    print(f"  {len(lines)}개 라인 생성")
    for i, l in enumerate(lines[:5]):
        print(f"  [{i:02d}] {l}")
    if len(lines) > 5:
        print(f"  ... 총 {len(lines)}개")

    # ── Step 2: Whisper 타임스탬프 (tts_script 기준 initial_prompt)
    words = get_whisper_words(audio_path, tts_text)

    # ── Step 3: 음절수 비율 매핑
    print("🔗 타임스탬프 정렬 (음절수 비율 기반)...")
    captions = align_lines_to_timestamps(lines, words)

    # ── Step 4: SRT 출력
    write_srt(captions, srt_path)

    # ── Step 5: 장면 타이밍
    with open(scenes_path, "r", encoding="utf-8") as f:
        scenes = json.load(f)

    scenes = calc_scene_timing(scenes, words)

    with open(timed_path, "w", encoding="utf-8") as f:
        json.dump(scenes, f, ensure_ascii=False, indent=2)

    print("\n=== 장면 타이밍 ===")
    for i, s in enumerate(scenes):
        print(f"  [{i:02d}] {s['start']:.2f}s~{s['end']:.2f}s ({s['duration']:.2f}s) {s['visual_query']}")


if __name__ == "__main__":
    main()
