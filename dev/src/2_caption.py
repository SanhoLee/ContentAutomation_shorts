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

import difflib
import json
import os
import re

from faster_whisper import WhisperModel

WORK_DIR   = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
MAX_CHARS  = int(os.environ.get("CAPTION_MAX_CHARS", "13"))  # 한글 폰트 가로폭 기준 최적값
MIN_CHARS  = int(os.environ.get("CAPTION_MIN_CHARS", "6"))
MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")
CAPTION_OFFSET_SEC = float(os.environ.get("CAPTION_OFFSET_SEC", "-0.15"))


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

def _join_tokens(tokens: list[str]) -> str:
    """
    토큰 목록을 한국어 규칙에 맞게 문자열로 결합.
    조사/어미(_ATTACH)는 앞 단어에 공백 없이 붙이고,
    그 외 일반 어절은 공백으로 구분한다.
    """
    if not tokens:
        return ""
    result = tokens[0]
    for tok in tokens[1:]:
        if _ATTACH.match(tok):
            result += tok        # 조사: 공백 없이 직접 붙임
        else:
            result += " " + tok  # 일반 어절: 공백 유지
    return result


def split_script_to_lines(script_text: str) -> list[str]:
    """
    caption_script.txt를 한국어 문법 기반으로 자막 라인 분할.

    - 조사·어미(_ATTACH)는 앞 단어에 공백 없이 붙임 (원문 띄어쓰기 보존)
    - 문장 끝(_SENTENCE_END)에서 즉시 줄바꿈
    - 절 경계(_CLAUSE_BREAK) + 충분한 길이일 때 줄바꿈
    - MAX_CHARS 초과 시 강제 줄바꿈
    """
    tokens  = script_text.replace("\n", " ").replace("\r", " ").split()
    tokens  = [t for t in tokens if t]

    lines      = []
    cur_tokens = []   # 현재 라인에 쌓이는 토큰 목록
    cur_syl    = 0.0

    for i, token in enumerate(tokens):
        tok_syl = _syllables(token)

        # 조사/어미 → 공백 없이 앞 토큰에 붙임 (줄바꿈 트리거 검사 포함)
        if _ATTACH.match(token) and cur_tokens:
            cur_tokens.append(token)
            cur_syl += tok_syl
            joined = _join_tokens(cur_tokens)
            if _SENTENCE_END.search(joined):
                lines.append(joined)
                cur_tokens = []; cur_syl = 0.0
            continue

        # 최대 음절수 초과 → 현재 라인 마감
        if cur_syl + tok_syl > MAX_CHARS and cur_syl >= MIN_CHARS:
            line = _join_tokens(cur_tokens)
            if line:
                lines.append(line)
            cur_tokens = []; cur_syl = 0.0

        cur_tokens.append(token)
        cur_syl += tok_syl
        joined = _join_tokens(cur_tokens)

        # 다음 토큰이 조사면 여기서 끊지 않음
        next_tok = tokens[i + 1] if i + 1 < len(tokens) else ""
        if _ATTACH.match(next_tok):
            continue

        if _SENTENCE_END.search(joined):
            lines.append(joined)
            cur_tokens = []; cur_syl = 0.0
        elif _CLAUSE_BREAK.search(joined) and cur_syl >= MIN_CHARS:
            lines.append(joined)
            cur_tokens = []; cur_syl = 0.0

    if cur_tokens:
        line = _join_tokens(cur_tokens)
        if line:
            lines.append(line)

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

def apply_caption_offset(captions: list[dict], audio_end: float | None = None) -> list[dict]:
    if CAPTION_OFFSET_SEC == 0:
        return captions
    shifted = []
    prev_end = 0.0
    for cap in captions:
        start = max(0.0, cap["start"] + CAPTION_OFFSET_SEC)
        end = cap["end"] + CAPTION_OFFSET_SEC
        if audio_end is not None:
            end = min(end, audio_end)
        end = max(end, start + 0.3)
        if shifted and start < prev_end:
            start = prev_end
            end = max(end, start + 0.3)
        shifted.append({"text": cap["text"], "start": round(start, 3), "end": round(end, 3)})
        prev_end = shifted[-1]["end"]
    return shifted


def _normalize_for_alignment(text: str) -> str:
    """텍스트 매칭 전용 정규화: 공백/문장부호 차이를 제거한다."""
    text = text.lower()
    return "".join(re.findall(r"[0-9a-z가-힣]+", text))


def _window_text(words: list[dict], start_idx: int, end_idx: int) -> str:
    return _normalize_for_alignment("".join(w["word"] for w in words[start_idx:end_idx + 1]))


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        # 짧은 조사/어미 차이처럼 한쪽이 다른 쪽에 포함되면 보너스를 준다.
        ratio = max(ratio, min(len(a), len(b)) / max(len(a), len(b)))
    return ratio


def _fallback_end_idx(words: list[dict], start_idx: int, target_syl: float) -> int:
    idx = start_idx
    acc = 0.0
    while idx < len(words) and acc < target_syl:
        acc += max(_syllables(words[idx]["word"]), 0.5)
        idx += 1
    return max(min(idx, len(words)) - 1, start_idx)


def _find_best_word_window(words: list[dict], start_hint: int, target_text: str, target_syl: float) -> tuple[int, int, float]:
    """
    현재 위치 주변에서 caption/tts 라인과 가장 비슷한 Whisper word 구간을 찾는다.

    이전 구현은 라인 음절 수만큼 word_idx를 순차 소비했기 때문에 한 번 생긴
    배정 오차가 뒤 라인까지 누적될 수 있었다. 여기서는 최초 구현의 장점처럼
    실제 Whisper 단어 경계를 쓰되, 어떤 단어 구간이 현재 라인인지 텍스트
    유사도로 재앵커링한다.
    """
    if not words:
        return 0, 0, 0.0

    target_norm = _normalize_for_alignment(target_text)
    if not target_norm:
        end_idx = _fallback_end_idx(words, start_hint, target_syl)
        return start_hint, end_idx, 0.0

    n = len(words)
    start_min = max(0, start_hint - 2)
    start_max = min(n - 1, start_hint + 8)
    best = (start_hint, _fallback_end_idx(words, start_hint, target_syl), -1.0)

    # 예상 음절량의 대략적인 범위 안에서 후보 end를 만든다.
    for start_idx in range(start_min, start_max + 1):
        acc = 0.0
        for end_idx in range(start_idx, n):
            acc += max(_syllables(words[end_idx]["word"]), 0.5)
            if acc < max(1.0, target_syl * 0.45):
                continue

            window_norm = _window_text(words, start_idx, end_idx)
            score = _similarity(target_norm, window_norm)

            # start_hint에서 멀어질수록 약한 패널티를 줘서 단조 진행성을 유지한다.
            score -= abs(start_idx - start_hint) * 0.015

            if score > best[2]:
                best = (start_idx, end_idx, score)

            if acc >= target_syl * 1.8 or len(window_norm) > len(target_norm) * 2.2 + 8:
                break

    return best


def align_lines_to_timestamps(
    lines: list[str],
    words: list[dict],
    timing_lines: list[str] | None = None,
) -> list[dict]:
    """
    caption 라인을 Whisper 단어 타임라인에 텍스트 앵커로 정렬한다.

    표시 문자열은 caption_script 기반 lines를 유지하고, 타이밍 매칭은 가능하면
    TTS가 실제로 읽은 timing_lines를 사용한다. 매칭 신뢰도가 낮은 라인은 기존
    음절수 순차 소비 방식을 fallback으로 사용한다.
    """
    if not words:
        print("⚠️  단어 없음 — 균등 분할 fallback")
        t = 0.0
        result = []
        for line in lines:
            result.append({"text": line, "start": t, "end": t + 2.0})
            t += 2.0
        return apply_caption_offset(result)

    if timing_lines and len(timing_lines) != len(lines):
        print(
            "⚠️  표시용/발화용 라인 수 불일치 "
            f"({len(lines)} vs {len(timing_lines)}) — 표시 라인 기준으로 정렬"
        )
        timing_lines = None

    audio_end = words[-1]["end"]
    reference_lines = timing_lines or lines
    total_line_syl = sum(_syllables(l) for l in reference_lines) or 1.0
    total_word_syl = sum(_syllables(w["word"]) for w in words) or total_line_syl
    syl_ratio = total_word_syl / total_line_syl

    result = []
    word_idx = 0
    anchor_hits = 0
    fallback_hits = 0

    for i, line in enumerate(lines):
        timing_text = reference_lines[i] if i < len(reference_lines) else line
        start_hint = min(word_idx, len(words) - 1)
        target_syl = max(_syllables(timing_text) * syl_ratio, 1.0)

        start_idx, end_idx, score = _find_best_word_window(words, start_hint, timing_text, target_syl)

        # 너무 낮은 매칭은 오히려 엉뚱한 곳으로 점프할 수 있으므로 기존 방식으로 fallback.
        if score < 0.48:
            start_idx = start_hint
            end_idx = _fallback_end_idx(words, start_idx, target_syl)
            fallback_hits += 1
        else:
            anchor_hits += 1

        word_idx = min(end_idx + 1, len(words))
        start = words[start_idx]["start"]
        end = words[end_idx]["end"]

        if end - start < 0.3:
            end = min(start + 0.3, audio_end)
        if result and start < result[-1]["end"]:
            start = result[-1]["end"]
            end = max(end, start + 0.3)

        result.append({"text": line, "start": round(start, 3), "end": round(end, 3)})

    if result:
        result[-1]["end"] = round(audio_end, 3)

    result = apply_caption_offset(result, audio_end)
    print(f"  텍스트 앵커 정렬: anchor={anchor_hits}, fallback={fallback_hits}")
    print(f"  자막 오프셋 적용: {CAPTION_OFFSET_SEC:+.2f}s")
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
    timing_lines = split_script_to_lines(tts_text) if tts_text != caption_text else lines
    print(f"  표시용 {len(lines)}개 라인 생성")
    if timing_lines is not lines:
        print(f"  발화용 {len(timing_lines)}개 라인 생성")
    for i, l in enumerate(lines[:5]):
        print(f"  [{i:02d}] {l}")
    if len(lines) > 5:
        print(f"  ... 총 {len(lines)}개")

    # ── Step 2: Whisper 타임스탬프 (tts_script 기준 initial_prompt)
    words = get_whisper_words(audio_path, tts_text)

    # ── Step 3: 텍스트 앵커 기반 매핑
    print("🔗 타임스탬프 정렬 (텍스트 앵커 + 음절 fallback)...")
    captions = align_lines_to_timestamps(lines, words, timing_lines)

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
