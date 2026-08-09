"""The six Scene 1 hook patterns, and the one function that maps any historical
label onto one of them.

Unlike story_type, the pattern is CHOSEN BY CLAUDE (planner first, then Stage 2).
There is deliberately no mix, no quota and no picker here — monotony is already
priced by objective_planner's format_hook_repeat penalty, and a hook has to
follow the topic rather than a quota.

This module exists so that the planner, Stage 1, Stage 2 and content_features
argue about the same six words. Before it, three different vocabularies were in
use and only two labels overlapped, which is why hook performance could never be
aggregated: the label written at planning time was not the label Stage 2 saw.
"""

NUMBER = "숫자형"
CURIOSITY_GAP = "호기심갭형"
REVERSAL = "반전형"
CALLOUT = "지목형"
WARNING = "경고형"
EXPOSE = "폭로형"

HOOK_PATTERNS: tuple[str, ...] = (NUMBER, CURIOSITY_GAP, REVERSAL, CALLOUT, WARNING, EXPOSE)

# One line per pattern, written to be dropped straight into a prompt. NUMBER and
# CURIOSITY_GAP carry concreteness rules rather than just a description: a round
# number and a demonstrative-only tease are the two ways these patterns fail
# while still looking like they were followed.
HOOK_RULES: dict[str, str] = {
    NUMBER: (
        "구체적 실수치 한 개로 시작합니다. 근거 자료에서 확인된 숫자만 쓰고, "
        "'많은/대부분/상당수/적지 않은' 같은 뭉뚱그린 표현과 어림수('약 절반', '거의 모두')는 금지. "
        "예: '10명 중 7명이 모르고 지나갑니다'"
    ),
    CURIOSITY_GAP: (
        "결정적 사실 하나를 Scene 1에서 끝까지 감춥니다. 감추는 대상은 반드시 손에 잡히는 "
        "구체 명사로 지목하세요. '이 방법' ✗ → '아침에 드시는 이 한 잔' ✓, "
        "'그 이유' ✗ → '검사지 맨 아래 한 줄' ✓"
    ),
    REVERSAL: (
        "시청자가 원인이라 믿고 있는 것을 첫 문장에서 뒤집습니다. "
        "예: '원인이 여러분이 생각하는 그게 아닙니다'"
    ),
    CALLOUT: (
        "시청자가 방금 한 행동을 콕 집어 찌릅니다. 2인칭 현재형으로 쓰세요. "
        "예: '방금도 이거 하고 계셨죠?'"
    ),
    WARNING: (
        "지금 이 순간에도 손실이 진행 중임을 알립니다. 근거 없는 질병 확정 표현은 금지. "
        "예: '이 습관, 지금도 뇌를 조용히 갉아먹고 있습니다'"
    ),
    EXPOSE: (
        "아무도 먼저 말해주지 않는 사실이 있다고 알립니다. "
        "예: '의사들이 굳이 먼저 말 안 해주는 사실이 있습니다'"
    ),
}

# Every hook label this repo has ever written to strategy.json, topic_plan.json
# or content_features.hook_type. Read-side only — nothing writes these again.
# 공감형/도전형 both collapse onto 지목형: all three are second-person "you, right
# now" hooks, and 도전형 only ever existed as the wildcard fallback constant.
LEGACY_HOOKS: dict[str, str] = {
    "숫자충격형": NUMBER,
    "질문형": CURIOSITY_GAP,
    "공감형": CALLOUT,
    "도전형": CALLOUT,
    "즉각지목형": CALLOUT,
    "두려움형": WARNING,
    "손실회피형": WARNING,
    "손실회피·경각심형": WARNING,
    "경각심형": WARNING,
    "발견형": EXPOSE,
    "금기폭로형": EXPOSE,
}


def normalize(value) -> str | None:
    """A canonical hook pattern, or None.

    Accepts the legacy labels too, because old strategy.json files and the
    content_features rows written before the vocabularies were merged both show
    up here. Never raises and never guesses: an unrecognized string returns None
    so callers keep their existing skip/default behaviour.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if text in HOOK_PATTERNS:
        return text
    return LEGACY_HOOKS.get(text)


def describe(pattern) -> str:
    """'라벨 — 규칙' for one pattern, or an empty string if it is unknown."""
    resolved = normalize(pattern)
    if not resolved:
        return ""
    return f"{resolved} — {HOOK_RULES[resolved]}"


def prompt_block(assigned=None) -> str:
    """The six patterns as prompt bullets, the assigned one first.

    Stage 1, Stage 2 and the planner prompt all render from this, so the
    vocabulary cannot drift apart a fourth time.
    """
    resolved = normalize(assigned)
    ordered = list(HOOK_PATTERNS)
    if resolved:
        ordered.remove(resolved)
        ordered.insert(0, resolved)
    return "\n".join(f"  · {pattern}: {HOOK_RULES[pattern]}" for pattern in ordered)


__all__ = [
    "NUMBER", "CURIOSITY_GAP", "REVERSAL", "CALLOUT", "WARNING", "EXPOSE",
    "HOOK_PATTERNS", "HOOK_RULES", "LEGACY_HOOKS",
    "normalize", "describe", "prompt_block",
]
