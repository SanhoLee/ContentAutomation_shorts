"""story_types.py — the four narrative genres this channel produces, and the
deterministic rule that decides which one an auto-planned job gets.

The channel had one implicit story shape (hook → 공감 → 설명 → 실천) applied to
every topic regardless of what the topic actually was. story_type fixes four
shapes instead, each with its own scene-role sequence, and spreads them over
time according to a configured mix so the feed does not converge on one genre.

Two hard constraints from CLAUDE.md shape this module:

* Selection is deterministic Python. Claude never picks the story_type — it
  only writes inside the one it is handed. The picker is a largest-deficit
  apportionment over the recent history, which is reproducible and testable.
* Nothing here may fail a job. Missing config, an unreadable work directory or
  a garbage story_type all degrade to documented defaults rather than raising.

`format_type` (the pre-existing objective_planner axis) is not replaced. The
two are kept consistent through the mappings below, with story_type as the
source of truth when they disagree — see the design spec §3.
"""

from __future__ import annotations

import json
import contextlib
import os
from collections import Counter
from pathlib import Path
from typing import Iterable, Mapping, Sequence

# ── Identity ────────────────────────────────────────────────────────────────

PRINCIPLE_EXPERIENCE = "principle_experience"
MYTH_BUST = "myth_bust"
HABIT_MECHANISM = "habit_mechanism"
CASE_JOURNEY = "case_journey"

STORY_TYPES: tuple[str, ...] = (
    PRINCIPLE_EXPERIENCE,
    MYTH_BUST,
    HABIT_MECHANISM,
    CASE_JOURNEY,
)

STORY_TYPE_LABELS: dict[str, str] = {
    PRINCIPLE_EXPERIENCE: "원리·체험형",
    MYTH_BUST: "오해 교정형",
    HABIT_MECHANISM: "습관·기전형",
    CASE_JOURNEY: "사례·여정형",
}

STORY_TYPE_GOALS: dict[str, str] = {
    PRINCIPLE_EXPERIENCE: "현상의 역설 → 숨은 기전 → 짧은 확인",
    MYTH_BUST: "통념 한 줄 → 근거의 범위 → 대안",
    HABIT_MECHANISM: "습관 하나 ↔ 뇌 기전 → 오늘 할 최소 행동",
    CASE_JOURNEY: "전형 인물의 짧은 여정",
}

# ── format_type ↔ story_type ────────────────────────────────────────────────
# The reverse map is intentionally not a strict inverse: two formats collapse
# onto myth_bust and two onto principle_experience, so the reverse direction
# picks the single canonical format for each story_type (design spec §3).

FORMAT_TO_STORY: dict[str, str] = {
    "오해반전형": MYTH_BUST,
    "자가진단형": PRINCIPLE_EXPERIENCE,
    "비교형": MYTH_BUST,
    "연구발견형": PRINCIPLE_EXPERIENCE,
    "행동챌린지형": HABIT_MECHANISM,
    "사례추적형": CASE_JOURNEY,
}

STORY_TO_FORMAT: dict[str, str] = {
    PRINCIPLE_EXPERIENCE: "자가진단형",
    MYTH_BUST: "오해반전형",
    HABIT_MECHANISM: "행동챌린지형",
    CASE_JOURNEY: "사례추적형",
}

# ── Scene contract ──────────────────────────────────────────────────────────

ROLE_SEQUENCES: dict[str, tuple[str, ...]] = {
    PRINCIPLE_EXPERIENCE: ("hook", "familiar", "hidden", "mechanism", "proof", "reframe", "cta"),
    MYTH_BUST: ("hook", "why_believed", "evidence", "limit", "alternative", "contrast", "cta"),
    HABIT_MECHANISM: ("hook", "situation", "mechanism", "evidence", "minimal_action", "if_skip", "cta"),
    CASE_JOURNEY: ("hook", "stuck", "turning", "mechanism", "small_result", "general_lesson", "cta"),
}

VISUAL_TYPES: tuple[str, ...] = (
    "paradox", "scale", "mechanism", "proof", "habit", "contrast", "person", "aftermath", "broll",
)

# Last-resort English stock query for a scene that arrived without one. Generic
# and motion-carrying, matching the fallback pool in 3_broll.py.
FALLBACK_VISUAL_QUERY = "senior person daily life home"

# Which visual type a role defaults to when Stage 2 leaves it blank or invalid.
ROLE_DEFAULT_VISUAL: dict[str, str] = {
    "hook": "paradox",
    "familiar": "habit",
    "hidden": "mechanism",
    "mechanism": "mechanism",
    "proof": "proof",
    "reframe": "contrast",
    "why_believed": "habit",
    "evidence": "proof",
    "limit": "scale",
    "alternative": "contrast",
    "contrast": "contrast",
    "situation": "habit",
    "minimal_action": "habit",
    "if_skip": "aftermath",
    "stuck": "person",
    "turning": "person",
    "small_result": "aftermath",
    "general_lesson": "contrast",
    "cta": "broll",
}

# ── Mix configuration ───────────────────────────────────────────────────────

DEFAULT_CONFIG: dict = {
    "story_type_mix": {
        PRINCIPLE_EXPERIENCE: 0.35,
        MYTH_BUST: 0.30,
        HABIT_MECHANISM: 0.25,
        CASE_JOURNEY: 0.10,
    },
    "lookback_jobs": 20,
    "enforce_on_auto": True,
    "enforce_on_manual": False,
    "default_story_type": PRINCIPLE_EXPERIENCE,
    "priority_on_tie": list(STORY_TYPES),
}

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
DEFAULT_MIX_PATH = CONFIG_DIR / "story_type_mix.json"
DEFAULT_TEMPLATES_DIR = CONFIG_DIR / "story_templates"

_config_cache: dict[str, tuple[float, dict]] = {}


def normalize(value) -> str | None:
    """A valid story_type id, or None. Accepts the Korean label too, because
    Stage 1 output and hand-edited topic JSON both show up here."""
    text = str(value or "").strip()
    if not text:
        return None
    if text in STORY_TYPES:
        return text
    lowered = text.lower().replace("-", "_").replace(" ", "_")
    if lowered in STORY_TYPES:
        return lowered
    for story_type, label in STORY_TYPE_LABELS.items():
        if text == label:
            return story_type
    return None


def _normalize_mix(raw) -> dict[str, float]:
    """Keep only known ids with a positive weight, then renormalize to 1.0.

    Renormalizing rather than rejecting means an operator can disable a genre
    by setting it to 0 (or deleting the key) without also having to rebalance
    the remaining three by hand.
    """
    mix = {}
    for story_type in STORY_TYPES:
        try:
            weight = float((raw or {}).get(story_type, 0.0))
        except (TypeError, ValueError):
            weight = 0.0
        if weight > 0:
            mix[story_type] = weight
    if not mix:
        return dict(DEFAULT_CONFIG["story_type_mix"])
    total = sum(mix.values())
    return {story_type: weight / total for story_type, weight in mix.items()}


def _coerce_config(raw: Mapping) -> dict:
    config = dict(DEFAULT_CONFIG)
    config["story_type_mix"] = _normalize_mix(raw.get("story_type_mix"))
    with contextlib.suppress(TypeError, ValueError):
        config["lookback_jobs"] = max(1, int(raw.get("lookback_jobs", DEFAULT_CONFIG["lookback_jobs"])))
    for flag in ("enforce_on_auto", "enforce_on_manual"):
        if flag in raw:
            config[flag] = bool(raw[flag])
    config["default_story_type"] = (
        normalize(raw.get("default_story_type")) or DEFAULT_CONFIG["default_story_type"]
    )
    priority = [normalize(item) for item in (raw.get("priority_on_tie") or [])]
    priority = [item for item in priority if item]
    # Any id the operator left out still needs a defined tie position, so the
    # channel default order fills the tail.
    config["priority_on_tie"] = priority + [t for t in STORY_TYPES if t not in priority]
    return config


def load_config(path=None) -> dict:
    """story_type_mix.json, cached by mtime like creative_dna.md.

    Never raises: a missing or malformed file yields DEFAULT_CONFIG, which is
    the same mix the file ships with.
    """
    target = str(path or os.environ.get("STORY_TYPE_MIX_PATH") or DEFAULT_MIX_PATH)
    try:
        mtime = os.path.getmtime(target)
    except OSError:
        return dict(DEFAULT_CONFIG)
    cached = _config_cache.get(target)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        with open(target, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_CONFIG)
    if not isinstance(raw, Mapping):
        return dict(DEFAULT_CONFIG)
    config = _coerce_config(raw)
    _config_cache[target] = (mtime, config)
    return config


# ── format/story reconciliation ─────────────────────────────────────────────

def story_type_for_format(format_type) -> str | None:
    return FORMAT_TO_STORY.get(str(format_type or "").strip())


def format_for_story_type(story_type) -> str:
    return STORY_TO_FORMAT.get(normalize(story_type) or "", STORY_TO_FORMAT[PRINCIPLE_EXPERIENCE])


def reconcile(story_type, format_type, config=None) -> tuple[str, str, str | None]:
    """Settle a (story_type, format_type) pair into a consistent one.

    Returns (story_type, format_type, warning). story_type wins when both are
    present and inconsistent — the format is rewritten to the mapped value and
    the warning describes the rewrite so the caller can log it (spec §3).
    An absent story_type is derived from the format instead, which is how
    pre-story_type jobs and hand-written topic JSON keep working.
    """
    config = config or load_config()
    resolved = normalize(story_type)
    raw_format = str(format_type or "").strip()

    if resolved is None:
        resolved = story_type_for_format(raw_format) or config["default_story_type"]
        return resolved, (raw_format or format_for_story_type(resolved)), None

    if not raw_format:
        return resolved, format_for_story_type(resolved), None

    if FORMAT_TO_STORY.get(raw_format) == resolved:
        return resolved, raw_format, None

    mapped = format_for_story_type(resolved)
    return resolved, mapped, (
        f"format_type '{raw_format}'이 story_type '{resolved}'와 맞지 않아 "
        f"'{mapped}'로 정규화했습니다."
    )


# ── Recent history ──────────────────────────────────────────────────────────

def work_root(explicit=None) -> Path | None:
    """Directory that holds one subdirectory per job.

    config.sh exports WORK_DIR as the *job* directory (WORK_DIR_BASE/JOB_ID),
    so the root is WORK_DIR_BASE when set and the parent of WORK_DIR otherwise.
    """
    if explicit:
        return Path(explicit)
    base = os.environ.get("WORK_DIR_BASE")
    if base:
        return Path(base)
    work_dir = os.environ.get("WORK_DIR")
    if work_dir:
        return Path(work_dir).parent
    fallback = Path(os.path.expanduser("~/brain50/data/work"))
    return fallback if fallback.is_dir() else None


def recent_story_types(root=None, limit: int | None = None, format_rows: Iterable = ()) -> list[str]:
    """story_type of the most recent jobs, newest first.

    Source order follows the spec §4.3: strategy.json under the work root
    first, then a `format_type` backfill for jobs that predate story_type
    (passed in by the caller, which owns the feedback DB connection). Jobs with
    neither are skipped rather than guessed at — an unknown genre must not
    count against any bucket's quota.
    """
    config = load_config()
    limit = limit or config["lookback_jobs"]
    collected: list[str] = []

    root_path = work_root(root)
    if root_path is not None:
        try:
            job_dirs = [entry for entry in root_path.iterdir() if entry.is_dir()]
        except OSError:
            job_dirs = []
        job_dirs.sort(key=lambda entry: _safe_mtime(entry / "strategy.json"), reverse=True)
        for job_dir in job_dirs:
            if len(collected) >= limit:
                break
            strategy_path = job_dir / "strategy.json"
            if not strategy_path.is_file():
                continue
            try:
                strategy = json.loads(strategy_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
            if not isinstance(strategy, Mapping):
                continue
            design = strategy.get("content_design")
            design = design if isinstance(design, Mapping) else {}
            resolved = (
                normalize(strategy.get("story_type"))
                or normalize(design.get("story_type"))
                or story_type_for_format(strategy.get("format_type") or design.get("format_type"))
            )
            if resolved:
                collected.append(resolved)

    for row in format_rows:
        if len(collected) >= limit:
            break
        resolved = story_type_for_format(row)
        if resolved:
            collected.append(resolved)

    return collected[:limit]


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# ── Selection ───────────────────────────────────────────────────────────────

def deficits(recent: Sequence[str], config=None) -> dict[str, float]:
    """How far behind its target share each genre is, counting the job about
    to be made. Positive means under-produced, negative over-produced."""
    config = config or load_config()
    mix = config["story_type_mix"]
    counts = Counter(story_type for story_type in recent if story_type in mix)
    horizon = len(recent) + 1
    return {story_type: weight * horizon - counts[story_type] for story_type, weight in mix.items()}


def pick_story_type(recent: Sequence[str], config=None, suggested=None, rng=None) -> str:
    """The genre this job should be, given what the recent jobs already were.

    Largest-deficit apportionment: whichever genre is furthest below its target
    share wins, ties broken by the configured channel priority. Run repeatedly
    this reproduces the configured mix, and it structurally caps the rare
    genres — case_journey at 0.10 cannot come up two jobs running unless the
    other three are all over quota.

    `suggested` (an eligible candidate's suggested_story_types) narrows the
    pool rather than overriding the mix, so a hint still cannot flood one
    genre. Unknown or fully over-quota suggestions fall back to the global
    pick instead of returning nothing.

    `rng` is used only for the cold-start case (no history at all), where every
    deficit is just the raw mix weight and a deterministic pick would make
    every fresh install start with the same genre. Omit it for reproducibility.
    """
    config = config or load_config()
    mix = config["story_type_mix"]
    priority = config["priority_on_tie"]

    scores = deficits(recent, config)
    pool = [story_type for story_type in (normalize(item) for item in (suggested or [])) if story_type in mix]
    # A hint only reorders within what the quota already allows. Suggestions
    # that are all over quota are dropped entirely rather than honoured, so a
    # run of myth-flavoured eligible titles cannot bend the mix.
    pool = [story_type for story_type in pool if scores[story_type] > 0]
    if not pool:
        pool = list(mix)

    if not recent and rng is not None:
        weights = [mix[story_type] for story_type in pool]
        return _weighted_choice(pool, weights, rng)

    return min(pool, key=lambda story_type: (-scores[story_type], priority.index(story_type)))


def _weighted_choice(pool: Sequence[str], weights: Sequence[float], rng) -> str:
    total = sum(weights)
    if total <= 0:
        return pool[0]
    threshold = rng.random() * total
    running = 0.0
    for story_type, weight in zip(pool, weights, strict=False):
        running += weight
        if running >= threshold:
            return story_type
    return pool[-1]


def next_story_type(root=None, suggested=None, format_rows: Iterable = (), rng=None) -> str:
    """pick_story_type over the live work directory — the one-call entry point
    for the planner."""
    config = load_config()
    recent = recent_story_types(root, config["lookback_jobs"], format_rows)
    return pick_story_type(recent, config, suggested=suggested, rng=rng)


# Keyword shapes that hint at a genre. Deliberately a hint, not a decision:
# pick_story_type only uses these to narrow the pool, so a false positive costs
# at most one job's genre and never breaks the configured mix.
_SUGGESTION_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (MYTH_BUST, ("오해", "잘못", "사실은", "진짜", "착각", "틀린", "속설", "아닙니다", "정말")),
    (HABIT_MECHANISM, ("습관", "매일", "하루", "아침", "저녁", "루틴", "실천", "챌린지", "일주일")),
    (CASE_JOURNEY, ("사례", "경험", "후기", "이야기", "바뀐", "겪은")),
    (PRINCIPLE_EXPERIENCE, ("왜", "원리", "이유", "때문", "메커니즘", "작동", "구조", "차이")),
)


def suggest_from_text(text) -> list[str]:
    """Genres a topic phrase leans toward, strongest first, possibly empty.

    Written onto eligible candidates so the picker has something to narrow on
    when that candidate is eventually consumed (spec §2.4).
    """
    haystack = str(text or "")
    if not haystack.strip():
        return []
    scored = []
    for story_type, keywords in _SUGGESTION_HINTS:
        hits = sum(1 for keyword in keywords if keyword in haystack)
        if hits:
            scored.append((hits, story_type))
    scored.sort(key=lambda item: (-item[0], STORY_TYPES.index(item[1])))
    return [story_type for _, story_type in scored]


def distribution(recent: Sequence[str]) -> dict[str, float]:
    """Observed share per genre; used by tests and the ops report."""
    total = len(recent)
    if not total:
        return dict.fromkeys(STORY_TYPES, 0.0)
    counts = Counter(recent)
    return {story_type: counts[story_type] / total for story_type in STORY_TYPES}


# ── Prompt fragments ────────────────────────────────────────────────────────

_template_cache: dict[str, tuple[float, str]] = {}


def templates_dir(explicit=None) -> Path:
    return Path(explicit or os.environ.get("STORY_TEMPLATES_DIR") or DEFAULT_TEMPLATES_DIR)


def load_template(story_type, directory=None) -> str:
    """The Stage 2 skeleton for one genre, cached by mtime so an operator edit
    lands on the next job with no restart. Missing file yields ""."""
    resolved = normalize(story_type)
    if resolved is None:
        return ""
    return _load_markdown(templates_dir(directory) / f"{resolved}.md")


def load_common_template(directory=None) -> str:
    """Rules shared by all four genres (`_common.md`)."""
    return _load_markdown(templates_dir(directory) / "_common.md")


def _load_markdown(path: Path) -> str:
    target = str(path)
    try:
        mtime = os.path.getmtime(target)
    except OSError:
        return ""
    cached = _template_cache.get(target)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    _template_cache[target] = (mtime, content)
    return content


def role_sequence(story_type) -> tuple[str, ...]:
    return ROLE_SEQUENCES.get(normalize(story_type) or "", ROLE_SEQUENCES[PRINCIPLE_EXPERIENCE])


def default_visual_type(role) -> str:
    return ROLE_DEFAULT_VISUAL.get(str(role or "").strip(), "broll")


def describe(story_type) -> str:
    resolved = normalize(story_type) or DEFAULT_CONFIG["default_story_type"]
    return f"{resolved}({STORY_TYPE_LABELS[resolved]}) — {STORY_TYPE_GOALS[resolved]}"


__all__ = [
    "STORY_TYPES", "STORY_TYPE_LABELS", "STORY_TYPE_GOALS", "FORMAT_TO_STORY", "STORY_TO_FORMAT",
    "ROLE_SEQUENCES", "VISUAL_TYPES", "DEFAULT_CONFIG",
    "normalize", "load_config", "story_type_for_format", "format_for_story_type", "reconcile",
    "work_root", "recent_story_types", "deficits", "pick_story_type", "next_story_type",
    "distribution", "load_template", "load_common_template", "role_sequence",
    "default_visual_type", "describe", "suggest_from_text", "FALLBACK_VISUAL_QUERY",
]
