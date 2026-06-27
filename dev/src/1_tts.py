"""
1_tts.py — TTS 음성 생성 + 텍스트 정규화

두 가지 정규화 버전을 생성한다:

  tts_script.txt     TTS 입력용 — 모든 기호 + 영어 약어를 한국어 발음으로 변환
                     ("LDL" → "엘디엘", "30퍼센트" → "30퍼센트")
                     TTS 엔진이 예측 가능하게 읽을 수 있는 순수 한국어 텍스트

  caption_script.txt 자막 표시용 — 기호만 변환, 영어 약어는 영어 그대로 유지
                     ("LDL" → "LDL", "30%" → "30퍼센트")
                     시청자가 읽기에 자연스러운 텍스트

voice.wav는 tts_script.txt 기준으로 생성되며,
2_caption.py는 caption_script.txt를 자막 표시 원본으로 사용한다.
"""

import os
import re
import shutil
import subprocess

WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
TTS_BIN  = os.environ.get("TTS_BIN") or os.environ.get("SUPERTONIC_BIN") or "supertonic"


# ─────────────────────────────────────────────
# 영어 약어 테이블
# key   = 원문 (대소문자 정확히 일치)
# value = TTS 발음 (한국어)
# 자막에는 key(영어 원문)를 그대로 표시
# ─────────────────────────────────────────────
ENGLISH_ABBREVS = {
    # 지질·대사
    "LDL":   "엘디엘",
    "HDL":   "에이치디엘",
    "VLDL":  "브이엘디엘",
    # 생체분자
    "DNA":   "디엔에이",
    "RNA":   "알엔에이",
    "ATP":   "에이티피",
    # 영상·검사
    "MRI":   "엠알아이",
    "CT":    "씨티",
    "PET":   "펫",
    "EEG":   "이이지",
    # 지표
    "BMI":   "비엠아이",
    "MMSE":  "엠엠에스이",
    # 치매 관련 바이오마커
    "pTau":  "피타우",
    "pTau217": "피타우이일칠",
    "ApoE":  "아포이",
    "APOE":  "아포이",
    "Abeta": "아밀로이드베타",
    # 질환
    "ADHD":  "에이디에이치디",
    "PTSD":  "피티에스디",
    # 기타 의학
    "ACE":   "에이스",
    "CVD":   "씨브이디",
}

# 기호 → 텍스트 변환 규칙 (TTS·자막 공통 적용)
_SYMBOL_RULES = [
    # % 처리 — 숫자 뒤의 % → 퍼센트 (build_prompt 규칙으로 이미 변환됐을 수 있으나 방어적으로 유지)
    (r"(\d+(?:\.\d+)?)\s*%",      r"\1퍼센트"),
    # 소수점 — 숫자.숫자 → 숫자점숫자
    (r"(\d+)\.(\d+)",              r"\1점\2"),
    # 범위 ~ — 숫자~숫자 → 숫자에서 숫자
    (r"(\d+)\s*~\s*(\d+)",        r"\1에서 \2"),
    # 단독 ~ — ~숫자 → 숫자 정도
    (r"~(\d+)",                    r"\1 정도"),
    # 화살표·수학 기호 → 공백으로
    (r"[→←↑↓·×÷≥≤±≈]",          r" "),
    # 중복 공백 정리
    (r" {2,}",                     r" "),
]


def _apply_symbol_rules(text: str) -> str:
    for pattern, repl in _SYMBOL_RULES:
        text = re.sub(pattern, repl, text)
    return text.strip()


def normalize_for_tts(text: str) -> str:
    """
    TTS 전용 정규화.
    기호 변환 + 영어 약어를 한국어 발음으로 치환.
    TTS 엔진이 예측 가능하게 읽도록 순수 한국어에 가깝게 변환.
    """
    text = _apply_symbol_rules(text)
    # 긴 약어부터 처리 (pTau217 이 pTau보다 먼저)
    for abbrev in sorted(ENGLISH_ABBREVS, key=len, reverse=True):
        pronunciation = ENGLISH_ABBREVS[abbrev]
        text = re.sub(r"(?<![A-Za-z])" + re.escape(abbrev) + r"(?![A-Za-z0-9])",
                      pronunciation, text)
    return text


def normalize_for_caption(text: str) -> str:
    """
    자막 전용 정규화.
    기호만 변환, 영어 약어는 영어 원문 그대로 유지.
    시청자가 읽기에 자연스러운 형태.
    """
    text = _apply_symbol_rules(text)
    # 영어 약어 → 변환하지 않음 (LDL → LDL)
    return text


# ─────────────────────────────────────────────
# TTS 실행 헬퍼
# ─────────────────────────────────────────────

def resolve_executable(command: str) -> str | None:
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


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────

def main():
    script_path  = os.path.join(WORK_DIR, "script.txt")
    tts_path     = os.path.join(WORK_DIR, "tts_script.txt")
    caption_path = os.path.join(WORK_DIR, "caption_script.txt")
    raw_path     = os.path.join(WORK_DIR, "voice_raw.wav")
    final_path   = os.path.join(WORK_DIR, "voice.wav")

    # ── 원본 스크립트 읽기
    with open(script_path, "r", encoding="utf-8") as f:
        raw_text = f.read().strip()

    # ── 정규화 두 버전 생성
    tts_text     = normalize_for_tts(raw_text)
    caption_text = normalize_for_caption(raw_text)

    with open(tts_path, "w", encoding="utf-8") as f:
        f.write(tts_text)
    with open(caption_path, "w", encoding="utf-8") as f:
        f.write(caption_text)

    print(f"✅ tts_script.txt     ({len(tts_text)}자) — TTS 입력용")
    print(f"✅ caption_script.txt ({len(caption_text)}자) — 자막 표시용")

    # 변환 diff 미리보기 (약어·기호 변환 확인용)
    if tts_text != caption_text:
        tts_words  = set(tts_text.split())
        cap_words  = set(caption_text.split())
        tts_only   = tts_words - cap_words
        if tts_only:
            print(f"   TTS 전용 변환 단어: {sorted(tts_only)[:8]}")

    # ── TTS 실행 (tts_script.txt 기준)
    tts_executable = resolve_executable(TTS_BIN)
    if not tts_executable:
        raise SystemExit(
            "TTS 실행 파일을 찾지 못했습니다. "
            "`which supertonic` 또는 `ls ~/.local/bin/supertonic`으로 설치 확인. "
            f"다른 경로라면 secrets.sh에 TTS_BIN=/절대/경로/supertonic 설정. "
            f"현재 PATH={os.environ.get('PATH', '')}"
        )

    subprocess.run([
        tts_executable, "tts", tts_text,
        "-o", raw_path,
        "--lang", "ko",
        "--voice", os.environ.get("TTS_VOICE", "F1"),
    ], check=True)

    # ── 속도 후처리 (ATEMPO, 피치 유지)
    ATEMPO = float(os.environ.get("ATEMPO", "1.0"))

    if ATEMPO != 1.0:
        ffmpeg_bin = resolve_executable(os.environ.get("FFMPEG_BIN", "ffmpeg"))
        if not ffmpeg_bin:
            raise SystemExit("ffmpeg를 찾지 못했습니다. `sudo apt install ffmpeg`")
        subprocess.run([
            ffmpeg_bin, "-y", "-i", raw_path,
            "-filter:a", f"atempo={ATEMPO}",
            final_path,
        ], check=True)
        os.remove(raw_path)
    else:
        os.replace(raw_path, final_path)

    print(f"✅ voice.wav 생성 완료 (ATEMPO={ATEMPO})")


if __name__ == "__main__":
    main()
