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
import functools
import json
import os
import re

import korean_grammar as kg

WORK_DIR   = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
MAX_CHARS  = int(os.environ.get("CAPTION_MAX_CHARS", "24"))
MIN_CHARS  = int(os.environ.get("CAPTION_MIN_CHARS", "8"))
MAX_WORDS  = int(os.environ.get("CAPTION_MAX_WORDS", "10"))
LINE_MAX_UNITS = float(os.environ.get("CAPTION_LINE_MAX_UNITS", "13"))
MIN_CAPTION_DURATION = float(os.environ.get("CAPTION_MIN_DURATION_SEC", "0.8"))
MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")
CAPTION_OFFSET_SEC = float(os.environ.get("CAPTION_OFFSET_SEC", "-0.15"))


# ─────────────────────────────────────────────
# 한국어 문맥 기반 자막 분할
# ─────────────────────────────────────────────

_TERMINAL_PUNCT = re.compile(r"[.!?][\"'”’)]*$")
_JOSA_TAIL = re.compile(r"(?:이|가|은|는|을|를|의|에|도)[,]?$")
_TIME_SUFFIXES = ("주", "개월", "일", "시간", "분", "초", "년")

# 어미 등급별 분할 보너스 (korean_grammar.split_priority 3/2/1/0에 대응).
# 종결어미 뒤가 가장 좋고, 연결어미, 전성어미 순으로 약해진다.
_BREAK_BONUS = (0.0, 3.0, 6.0, 10.0)


def _syllables(text: str) -> float:
    ko = len(re.findall(r"[가-힣]", text))
    num = len(re.findall(r"[0-9]", text))
    eng = len(re.findall(r"[A-Za-z]", text))
    return ko + num + eng * 0.4


def _display_units(text: str) -> float:
    """1080px ASS 화면에서 한글 전각 폭을 1로 둔 보수적 가로폭 근사치."""
    units = 0.0
    for char in text:
        if "가" <= char <= "힣":
            units += 1.0
        elif char.isspace():
            units += 0.4
        elif char.isascii() and char.isalnum():
            units += 0.55
        else:
            units += 0.5
    return units


def _is_sentence_end(text: str) -> bool:
    text = text.strip()
    return bool(_TERMINAL_PUNCT.search(text)) or kg.is_sentence_final(text)


BLOCK_NONE, BLOCK_SOFT, BLOCK_HARD = 0, 1, 2


def _boundary_block(left: str, right: str) -> int:
    """두 어절 사이를 끊는 것이 얼마나 나쁜지 등급으로 돌려준다.

    HARD 는 의미가 깨지는 자리라 끊지 않는다. SOFT 는 어색할 뿐이라
    대안이 없으면 양보한다 — soft 목록은 짧은 어간을 prefix로 훑기 때문에
    과매칭이 전제되어 있고, 금지로 다루면 멀쩡한 경계까지 막힌다
    (korean_grammar 모듈 설명 참고).
    """
    left_clean = kg.strip_edges(left)
    right_clean = kg.strip_edges(right)

    # hard — "억제할 ‖ 수 있다는", "이런 ‖ 적 있으셨나요"를 막는다.
    if kg.starts_with_dependent_noun(right_clean):
        return BLOCK_HARD
    if left_clean.endswith(("이라고", "라고")) and right_clean.startswith("해서"):
        return BLOCK_HARD
    if right_clean in {"안", "안에", "이내", "동안"} and left_clean.endswith(_TIME_SUFFIXES):
        return BLOCK_HARD

    # 진짜 어미 경계는 아래 soft 규칙보다 항상 낫다.
    if kg.ends_with_ending(left_clean):
        return BLOCK_NONE
    if kg.is_auxiliary_verb(right_clean):   # "눌러 ‖ 버립니다"
        return BLOCK_SOFT
    if kg.is_no_split_after(left_clean):    # "그리고 ‖ ...", "정말 ‖ ..."
        return BLOCK_SOFT
    return BLOCK_NONE


def _block_level(tokens: list[str], index: int) -> int:
    """tokens[index-1]과 tokens[index] 사이 경계의 등급."""
    if index <= 0 or index >= len(tokens):
        return BLOCK_NONE
    if kg.breaks_compound_term(tokens, index):
        return BLOCK_HARD
    return _boundary_block(tokens[index - 1], tokens[index])


def _split_sentence_tokens(tokens: list[str]) -> list[list[str]]:
    """폭 제한 안에서 전체 문장의 경계 비용을 최소화해 고아 어절을 방지한다."""
    n = len(tokens)
    if not n:
        return []
    limit = min(float(MAX_CHARS), LINE_MAX_UNITS * 2)
    dp = [(float("inf"), []) for _ in range(n + 1)]
    dp[n] = (0.0, [])
    for i in range(n - 1, -1, -1):
        text = ""
        for j in range(i + 1, n + 1):
            text = " ".join(tokens[i:j])
            width = _display_units(text)
            if width > limit and j > i + 1:
                break
            block = _block_level(tokens, j)
            if block == BLOCK_HARD:
                continue
            shortfall = max(0.0, MIN_CHARS - _syllables(text))
            target = limit * 0.78
            cost = (width - target) ** 2 + shortfall ** 2 * 5
            if block == BLOCK_SOFT:
                cost += 120
            # 어절 수는 폭과 별개의 상한이다. 짧은 어절이 몰리면 폭은 여유가
            # 있어도 한눈에 안 들어오므로 초과분만 벌점을 준다.
            if len(tokens[i:j]) > MAX_WORDS:
                cost += (len(tokens[i:j]) - MAX_WORDS) ** 2 * 25
            if len(tokens[i:j]) == 1 and j < n:
                cost += 18
            # 폭이 limit 안이어도 두 줄로 균형이 안 잡히는 묶음이 있다. 실제
            # 줄바꿈 결과를 보고 넘치는 만큼 벌점을 줘야 DP가 그걸 피해간다.
            spill = _wrap_overflow(tuple(tokens[i:j]))
            if spill:
                cost += spill ** 2 * 8
            if j < n and _JOSA_TAIL.search(tokens[j - 1]):
                cost += 24
            if j < n:
                cost -= _BREAK_BONUS[kg.split_priority(tokens[j - 1])]
            total = cost + dp[j][0]
            if total < dp[i][0]:
                dp[i] = (total, [tokens[i:j], *dp[j][1]])
        if not dp[i][1]:
            dp[i] = (dp[min(i + 1, n)][0] + 100, [[tokens[i]], *dp[min(i + 1, n)][1]])
    return dp[0][1]


def _wrap_caption(tokens: list[str]) -> str:
    """한 자막 이벤트를 화면폭 안의 최대 두 줄로 의미 균형에 맞춰 배치한다."""
    text = " ".join(tokens)
    if _display_units(text) <= LINE_MAX_UNITS:
        return text
    # 어색한 자리(soft)라도 끊는 편이 화면 밖으로 넘치는 것보다 낫다. 한 줄
    # 분량만큼의 가짜 초과폭을 얹어 자연스러운 자리가 있으면 그쪽이 이기게 한다.
    choices = []
    for idx in range(1, len(tokens)):
        block = _block_level(tokens, idx)
        if block == BLOCK_HARD:
            continue
        left = " ".join(tokens[:idx])
        right = " ".join(tokens[idx:])
        lw, rw = _display_units(left), _display_units(right)
        overflow = max(0.0, lw - LINE_MAX_UNITS) + max(0.0, rw - LINE_MAX_UNITS)
        # 어색함은 폭과 다른 단위라 같은 항에 더할 수 없다. 화면을 넘기지 않는
        # 것이 먼저고, 그 다음이 끊는 자리, 마지막이 두 줄의 균형이다.
        awkward = 2.0 if block == BLOCK_SOFT else 0.0
        if _JOSA_TAIL.search(tokens[idx - 1]):
            awkward += 1.0
        short_line = max(0.0, MIN_CHARS - lw) ** 2 + max(0.0, MIN_CHARS - rw) ** 2
        # 줄바꿈도 어미 뒤가 가장 읽기 좋다. 동률일 때만 갈리도록 약하게 준다.
        ending = -_BREAK_BONUS[kg.split_priority(tokens[idx - 1])] * 0.1
        choices.append((overflow, awkward, short_line, ending + abs(lw - rw), left, right))
    if not choices:
        return text
    *_, left, right = min(choices)
    return left + "\n" + right


@functools.lru_cache(maxsize=4096)
def _wrap_overflow(tokens: tuple[str, ...]) -> float:
    """줄바꿈까지 끝낸 뒤에도 한 줄이 화면폭을 넘는 정도. DP에서 매 후보마다
    부르므로 캐시한다."""
    wrapped = _wrap_caption(list(tokens))
    widest = max(_display_units(part) for part in wrapped.split("\n"))
    return max(0.0, widest - LINE_MAX_UNITS)


def split_script_to_lines(script_text: str) -> list[str]:
    """원문 공백·문장·문단 경계를 보존하는 1~2줄 자막 이벤트를 만든다."""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", script_text) if part.strip()]
    captions = []
    for paragraph in paragraphs:
        sentence_tokens = []
        for token in paragraph.replace("\n", " ").split():
            sentence_tokens.append(token)
            if _is_sentence_end(token):
                captions.extend(_wrap_caption(group) for group in _split_sentence_tokens(sentence_tokens))
                sentence_tokens = []
        if sentence_tokens:
            captions.extend(_wrap_caption(group) for group in _split_sentence_tokens(sentence_tokens))
    return [caption for caption in captions if caption.strip()]

# ─────────────────────────────────────────────
# 2. Whisper → 단어 타임스탬프
# ─────────────────────────────────────────────

def get_whisper_words(audio_path: str, tts_script: str) -> list[dict]:
    from faster_whisper import WhisperModel
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
    result = stabilize_caption_durations(result)
    print(f"  텍스트 앵커 정렬: anchor={anchor_hits}, fallback={fallback_hits}")
    print(f"  자막 오프셋 적용: {CAPTION_OFFSET_SEC:+.2f}s")
    return result

def stabilize_caption_durations(captions: list[dict]) -> list[dict]:
    """너무 짧은 서술어 자막을 앞 문맥에 병합해 최소 가독 시간을 확보한다."""
    stable = []
    limit = min(float(MAX_CHARS), LINE_MAX_UNITS * 2)
    for cap in captions:
        item = dict(cap)
        duration = item["end"] - item["start"]
        if duration < MIN_CAPTION_DURATION and stable and not _is_sentence_end(stable[-1]["text"]):
            combined = stable[-1]["text"].replace("\n", " ") + " " + item["text"].replace("\n", " ")
            if _display_units(combined) <= limit:
                tokens = combined.split()
                stable[-1]["text"] = _wrap_caption(tokens)
                stable[-1]["end"] = item["end"]
                continue
        stable.append(item)
    return stable

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
