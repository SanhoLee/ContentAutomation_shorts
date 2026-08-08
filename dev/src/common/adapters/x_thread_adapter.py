#!/usr/bin/env python3
"""x_thread_adapter.py — content_package.json -> X(Twitter) thread draft.

Phase 4 of the Pareto design spec. Every tweet's raw text still comes from
a field content_package.py already computed. On top of that, one bounded,
non-retrying Claude Haiku call (see _humanize_with_claude) rewrites the
batch into a more casual, non-expert tone before truncation is applied --
X's audience here (20s-40s) is both younger and broader than the 50+
YouTube viewer, and script prose written for narration reads as
stiff/robotic in a tweet. The same call also writes an X-optimized title
for the lead tweet. If ANTHROPIC_API_KEY is unset, the budget guard trips,
or the call fails for any reason, this falls back silently to the original
rule-based text and a rule-based title (production continuity over a
downstream-only artifact). Posting is out of scope here — this only writes
a draft an operator copies by hand (or x_poster.py posts).

Source order is hook, then each key_point. These sentences are packed
greedily into as few tweets as fit under TWEET_MAX_CHARS (139 -- X weighs
each CJK character as 2 toward its 280-weighted-character cap, so
pure-Korean text tops out around 140, not the ~270 an ASCII tweet gets)
rather than one sentence per tweet, since most individual sentences here
run far shorter than the limit.

The script's CTA scene and its next-episode tease are deliberately NOT
posted. They are a Shorts outro ("오늘부터 시작해보세요. 다음 편에서는...")
and asking a reader who just finished the thread to go do one more thing
reads as unnatural on X and scatters the attention the thread just built.
The last tweet is a recap instead -- see _render_summary_tweet.

The lead tweet carries the X title on its own line above the first packed
group; the title does not have to match the Shorts title, it only has to
work as a scroll-stopper on X. Packing reserves the title's width from the
first group's budget so the combined lead tweet still fits.

Three things are banned from the thread text outright, and stripped from
both the rule-based source and the Claude rewrite: hashtags, URLs, and the
sources block. Hashtags and links suppress reach on X and read as spam on
a health account; the sources are still produced, but as `sources_text` in
the payload for a Slack DM the operator copy-pastes, not as a trailing
tweet nobody reads.

Sentences that trip the Phase 1 safety ban-list (topic_score's
ban_keywords — "완치", "보장", "기적", ...) are dropped rather than posted
with a softened claim, since a dropped sentence is trivially safe and a
softened one still risks an implied medical guarantee. The filter runs
again after the humanize rewrite, since a rewrite could reintroduce risky
phrasing that wasn't in the original.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

COMMON_DIR = Path(__file__).resolve().parents[1]
if str(COMMON_DIR) not in sys.path:
    sys.path.insert(0, str(COMMON_DIR))

import claude_cost
import content_package
import topic_score
import x_photo_card
from objective_planner import job_rng

# X weighs every CJK character as 2 toward its 280-weighted-character cap,
# so a pure-Korean post tops out around 280/2=140 chars, not the ~270-280
# an ASCII tweet gets. 139 leaves a 1-char margin.
TWEET_MAX_CHARS = 139
# The title shares the lead tweet with the first packed group, so it is
# capped well under TWEET_MAX_CHARS -- a 60-char title still leaves ~70
# chars of body, enough for a whole hook sentence.
X_TITLE_MAX_CHARS = 60
TITLE_SEPARATOR = "\n\n"
MAX_SOURCE_LINKS = 3
SOURCES_LABEL = "출처"

# The closing recap tweet. There is deliberately no fixed label constant
# here: a hardcoded "핵심만 정리하면" on every single thread is its own kind
# of robot tell. The lead line is written per-episode by Claude, and falls
# back to a curated pool keyed by story_type (see _pick_summary_lead).
SUMMARY_PHRASES_PATH = Path(__file__).resolve().parent / "x_thread_phrases.json"
SUMMARY_MAX_LINES = 3
SUMMARY_LINE_MAX_CHARS = 40
SUMMARY_LEAD_MAX_CHARS = 24
SUMMARY_BULLET = "- "

X_THREAD_HUMANIZE_MODEL = os.environ.get("X_THREAD_HUMANIZE_MODEL", "claude-haiku-4-5-20251001")
X_THREAD_HUMANIZE_TIMEOUT_SEC = int(os.environ.get("X_THREAD_HUMANIZE_TIMEOUT_SEC", "20"))
X_THREAD_HUMANIZE_MAX_TOKENS = 1024
FALSE_VALUES = {"0", "false", "off", "no"}

SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
JSON_OBJECT = re.compile(r"\{.*\}", re.S)
URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+", re.I)
HASHTAG_PATTERN = re.compile(r"(?<!\S)#\S+")

HUMANIZE_PROMPT = """다음은 유튜브 쇼츠 대본에서 뽑은 문장들이다. 이 문장들을 X(트위터) 스레드용으로 다시 쓰고, 스레드 첫 트윗에 올릴 제목도 함께 만들어라.
주제: {topic}
쇼츠 제목(참고용): {title}

읽는 사람:
- 20~40대. 쇼츠 시청자(50대 이상)보다 젊고, 건강 정보를 스스로 찾아보는 사람들이다.
- 이들이 실제로 공감할 맥락(수면 부족, 스트레스, 스마트폰, 업무 집중력 등)으로 풀어 쓴다.

말투:
- 전문가/방송 멘트처럼 딱딱하지 않게, 평소 트위터에 편하게 쓰는 자연스러운 문장으로 바꾼다.
- 반드시 존댓말로 쓴다. 반말은 절대 쓰지 않는다. 격식만 차린 "~하십시오"체 대신 편한 해요체를 기본으로 한다.
- 완벽한 문장보다 사람이 쓴 것처럼 자연스러운 게 우선이다.
- 과장, 의학적 단정("완치", "보장", "기적" 등)은 절대 쓰지 않는다.

제목:
- 쇼츠 제목과 같을 필요 없다. X 타임라인에서 손이 멈추게 만드는 한 줄이면 된다.
- {title_max}자 이내. 본문 첫 문장을 그대로 베끼지 않는다.
- 존댓말 기조를 지키고, 낚시성 과장이나 의학적 단정은 쓰지 않는다.

마지막 요약(스레드를 닫는 트윗):
- summary: 위 문장들의 핵심만 1~3줄로 압축한다. 줄당 {line_max}자 이내.
- 본문에 없는 새로운 정보나 숫자를 만들어내지 않는다. 있는 내용만 줄인다.
- summary_lead: 그 요약을 여는 짧은 한 줄. {lead_max}자 이내.
- summary_lead는 이 편의 내용과 결에 맞게 매번 새로 쓴다. "핵심만 정리하면" 같은
  상투구를 기계적으로 반복하지 않는다.
- 요약과 머리말 모두 행동 유도("~해보세요")나 다음 편 예고를 넣지 않는다.
  이 트윗은 CTA가 아니라 읽은 내용을 한 번 더 정리해 주는 자리다.

공통 금지:
- 해시태그(#), URL/링크, 이모지는 절대 넣지 않는다.
- 각 문장의 핵심 정보와 순서, 문장 개수를 그대로 유지한다. 문장을 합치거나 나누지 않는다.

문장 목록:
{numbered}

출력은 순수 JSON 객체만. 형식:
{{"title": "제목", "sentences": ["문장1", "문장2"], "summary_lead": "머리말", "summary": ["요약1", "요약2"]}}
sentences 배열의 길이는 반드시 {count}개여야 한다. 다른 설명은 절대 붙이지 않는다."""


def _ban_keywords() -> list[str]:
    try:
        return list(topic_score.load_rules().get("ban_keywords") or ())
    except Exception:
        return []


def _humanize_enabled() -> bool:
    value = os.environ.get("X_THREAD_HUMANIZE")
    if value is None or value == "":
        return True
    return value.strip().lower() not in FALSE_VALUES


def _strip_banned_markup(text: str) -> str:
    """Remove hashtags and URLs, whatever their source.

    Applied to the rule-based text and to the Claude rewrite alike: the
    prompt asks for neither, but a rewrite that slips one in must not put a
    link or a "#뇌건강" on a live post. Collapsing whitespace afterwards
    keeps the gap left by a removed token from becoming a double space.
    """
    if not text:
        return ""
    text = URL_PATTERN.sub(" ", text)
    text = HASHTAG_PATTERN.sub(" ", text)
    return " ".join(text.split())


def _humanize_with_claude(raw_texts: list[str], *, topic: str, title: str) -> dict[str, Any] | None:
    """One bounded, non-retrying rewrite pass, returning
    {"title": str, "texts": list[str], "summary_lead": str, "summary": list}.
    Returns None on any failure so the caller can fall back to the original
    rule-based text rather than blocking a downstream-only artifact on a
    Claude outage."""
    if not raw_texts:
        return None
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        claude_cost.assert_budget(anticipated_cost_usd=0.01)
    except RuntimeError:
        return None

    import requests

    numbered = "\n".join(f"{i}. {text}" for i, text in enumerate(raw_texts, start=1))
    prompt = HUMANIZE_PROMPT.format(
        topic=topic or "뇌 건강",
        title=title or "(없음)",
        title_max=X_TITLE_MAX_CHARS,
        line_max=SUMMARY_LINE_MAX_CHARS,
        lead_max=SUMMARY_LEAD_MAX_CHARS,
        numbered=numbered,
        count=len(raw_texts),
    )
    payload = {
        "model": X_THREAD_HUMANIZE_MODEL,
        "max_tokens": X_THREAD_HUMANIZE_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    try:
        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers, json=payload, timeout=X_THREAD_HUMANIZE_TIMEOUT_SEC,
        )
        res.raise_for_status()
        data = res.json()
    except Exception:
        return None

    with contextlib.suppress(Exception):
        claude_cost.record_usage("x_thread_humanize", X_THREAD_HUMANIZE_MODEL, data)

    text = "".join(block.get("text", "") for block in data.get("content") or [] if block.get("type") == "text")
    match = JSON_OBJECT.search(text)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    sentences = parsed.get("sentences")
    if not isinstance(sentences, list) or len(sentences) != len(raw_texts):
        return None
    summary = parsed.get("summary")
    return {
        # A missing/empty title, lead or summary is survivable on its own --
        # each has a rule-based fallback, and the caller still adopts the
        # rewritten body. Only a sentence-count mismatch (above) is fatal,
        # because that means the body no longer maps onto the source.
        "title": str(parsed.get("title") or "").strip(),
        "texts": [str(t).strip() for t in sentences],
        "summary_lead": str(parsed.get("summary_lead") or "").strip(),
        "summary": [str(line).strip() for line in summary] if isinstance(summary, list) else [],
    }


def _apply_safety_filter(text: str, ban_keywords: list[str]) -> str:
    """Drop a sentence outright if it trips a ban keyword -- no softened
    rewrite, since that still risks an implied guarantee slipping through."""
    if not text:
        return ""
    lowered = text.lower()
    if any(bad.lower() in lowered for bad in ban_keywords if bad):
        return ""
    return text


def _sanitize(value: Any, ban_keywords: list[str], max_chars: int | None = None) -> str:
    """Every text field posted to X goes through the same three steps --
    strip hashtags/URLs, drop it if it trips a ban keyword, optionally
    truncate at a boundary. Every call site below wants this exact
    sequence, just with a different (or no) max_chars."""
    text = _apply_safety_filter(_strip_banned_markup(str(value or "").strip()), ban_keywords)
    if text and max_chars is not None:
        text = _truncate_at_boundary(text, max_chars)
    return text


def _truncate_at_boundary(text: str, max_chars: int) -> str:
    """Cut at the last sentence end within max_chars, else the last space,
    else a hard cut. No ellipsis -- spec calls for "의미 단위로 축약", not "...". """
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    window = text[:max_chars]
    matches = list(SENTENCE_END.finditer(window))
    if matches:
        return window[: matches[-1].end()].strip()
    last_space = window.rfind(" ")
    if last_space > 0:
        return window[:last_space].strip()
    return window.strip()


# "99/99 " -- reserving this fixed width for the numeric prefix during
# packing (rather than the real prefix, which depends on the final tweet
# count that isn't known until packing is done) is always >= the real
# prefix for any thread under 100 tweets, so every packed tweet still
# comes in under budget once the real, shorter prefix is applied.
_PREFIX_RESERVE_WIDTH = len("99/99 ")


def _pack_sentences(
    texts: list[str], *, max_chars: int, prefix_reserve: int, lead_reserve: int = 0,
) -> list[str]:
    """Greedily fill each tweet with as many whole sentences as fit,
    instead of one sentence per tweet -- a 70-char sentence alone in a
    139-char tweet wastes half the post. A single sentence longer than
    the budget is truncated at a boundary rather than left oversized.

    `lead_reserve` shrinks the first group's budget only: the lead tweet
    also has to carry the X title, so it has less room for body text than
    every tweet after it."""
    base = max(1, max_chars - prefix_reserve)

    def budget_for(group_index: int) -> int:
        return max(1, base - lead_reserve) if group_index == 0 else base

    groups: list[str] = []
    current = ""
    for text in texts:
        text = text.strip()
        if not text:
            continue
        budget = budget_for(len(groups))
        candidate = f"{current} {text}".strip() if current else text
        if len(candidate) <= budget:
            current = candidate
            continue
        if current:
            groups.append(current)
            budget = budget_for(len(groups))
        current = _truncate_at_boundary(text, budget) if len(text) > budget else text
    if current:
        groups.append(current)
    return groups


def _pubmed_url(pmid: str) -> str:
    return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"


def _source_lines_from_citations(
    citations: list[dict[str, Any]], ban_keywords: list[str],
) -> list[str]:
    lines: list[str] = []
    for citation in (citations or [])[:MAX_SOURCE_LINKS]:
        pmid = str((citation or {}).get("pmid") or "").strip()
        if not pmid:
            continue
        journal = str((citation or {}).get("journal") or "").strip()
        year = str((citation or {}).get("year") or "").strip()
        label = " ".join(p for p in (journal, year) if p)
        line = f"{label} {_pubmed_url(pmid)}".strip() if label else _pubmed_url(pmid)
        line = _apply_safety_filter(line, ban_keywords)
        if line:
            lines.append(line)
    return lines


def _source_lines_from_evidence(
    evidence: list[dict[str, Any]], ban_keywords: list[str],
) -> list[str]:
    lines: list[str] = []
    for item in (evidence or [])[:MAX_SOURCE_LINKS]:
        hint = _apply_safety_filter(str((item or {}).get("source_hint") or "").strip(), ban_keywords)
        if hint:
            lines.append(f"- {hint}")
    return lines


def build_sources_text(
    package: dict[str, Any],
    pubmed_citations: list[dict[str, Any]] | None = None,
    ban_keywords: list[str] | None = None,
) -> str:
    """Citations block for the operator's Slack DM — never a tweet.

    PMID links when available (real and verifiable), else evidence's
    source_hint text. Returns "" when neither exists, so the caller sends
    no DM rather than an empty reference. Unlike the thread text this may
    contain URLs: it is copy-paste reference material for a human, not
    something posted to X, where a link costs reach.
    """
    ban_keywords = ban_keywords if ban_keywords is not None else _ban_keywords()
    lines = _source_lines_from_citations(pubmed_citations or [], ban_keywords)
    if not lines:
        lines = _source_lines_from_evidence(package.get("evidence") or [], ban_keywords)
    if not lines:
        return ""
    return SOURCES_LABEL + "\n" + "\n".join(lines)


def _load_pubmed_citations(job_dir: Path) -> list[dict[str, Any]]:
    try:
        status = json.loads((job_dir / "pubmed_status.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    citations = status.get("citations")
    return list(citations) if isinstance(citations, list) else []


def _fallback_title(package: dict[str, Any], ban_keywords: list[str]) -> str:
    """Rule-based X title for when the Claude rewrite is off or failed.

    Prefers the Shorts title (already written to grab attention), then the
    core message, then the topic. Whatever is picked is stripped of
    hashtags/URLs and safety-filtered like any other posted text."""
    source = package.get("source") or {}
    candidates = (
        source.get("title"),
        package.get("core_message"),
        source.get("topic"),
        package.get("topic"),
    )
    for candidate in candidates:
        text = _sanitize(candidate, ban_keywords, X_TITLE_MAX_CHARS)
        if text:
            return text
    return ""


_summary_phrases_cache: dict[str, Any] | None = None


def _load_summary_phrases() -> dict[str, Any]:
    """Curated lead-in pool, read once and cached.

    A missing or corrupt file yields {} rather than raising: the thread
    then closes with the recap lines and no lead-in, which is a smaller
    loss than failing a downstream-only artifact over a data file.
    """
    global _summary_phrases_cache
    if _summary_phrases_cache is None:
        try:
            loaded = json.loads(SUMMARY_PHRASES_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            loaded = {}
        _summary_phrases_cache = loaded if isinstance(loaded, dict) else {}
    return _summary_phrases_cache


def _pick_summary_lead(package: dict[str, Any], job_id: str, ban_keywords: list[str]) -> str:
    """Rule-based lead-in for the recap tweet, varied per job.

    Deliberately not one hardcoded phrase: the same opener on every thread
    reads as canned. The pool is bucketed by story_type because a myth-bust
    and a habit piece close differently, and the pick is seeded from job_id
    via objective_planner.job_rng -- so consecutive episodes differ, but a
    given job always rebuilds the identical thread (re-running a job must
    not silently reword what a human already reviewed).
    """
    phrases = _load_summary_phrases()
    story_type = str(package.get("story_type") or "").strip()
    buckets = phrases.get("by_story_type") or {}
    pool = buckets.get(story_type) or phrases.get("default") or []
    pool = [str(phrase).strip() for phrase in pool if str(phrase).strip()]
    if not pool:
        return ""
    return _sanitize(job_rng(job_id).choice(pool), ban_keywords, SUMMARY_LEAD_MAX_CHARS)


def _summary_lines(
    package: dict[str, Any], ban_keywords: list[str], claude_summary: list[str] | None,
) -> list[str]:
    """The recap body: Claude's condensed lines, else the core message.

    core_message is Stage 1's "딱 한 문장" (the single sentence a viewer
    should leave with, capped at 30 chars), so it is exactly the right
    fallback for a recap that has to stand alone in one line.
    """
    raw = list(claude_summary or []) or [package.get("core_message")]
    lines = [_sanitize(value, ban_keywords, SUMMARY_LINE_MAX_CHARS) for value in raw[:SUMMARY_MAX_LINES]]
    return [line for line in lines if line]


def _render_summary_tweet(lead: str, lines: list[str], *, prefix_reserve: int) -> str:
    """Assemble the recap tweet, dropping content until it fits.

    Bullets only appear from two lines up -- a single recap line reads as a
    sentence, not a list of one. Overflow drops whole trailing lines before
    it resorts to cutting one, since a dropped point is cleaner than a
    half-sentence.
    """
    if not lines:
        return ""
    budget = max(1, TWEET_MAX_CHARS - prefix_reserve)
    lines = list(lines)
    while lines:
        body = [f"{SUMMARY_BULLET}{line}" for line in lines] if len(lines) > 1 else [lines[0]]
        text = "\n".join(([lead] if lead else []) + body)
        if len(text) <= budget:
            return text
        if len(lines) > 1:
            lines.pop()
            continue
        # One line left and still over: cut it rather than return nothing.
        head = f"{lead}\n" if lead else ""
        return head + _truncate_at_boundary(lines[0], max(1, budget - len(head)))
    return ""


def build_tweets(
    package: dict[str, Any], *, number_prefix: bool = False, ban_keywords: list[str] | None = None,
    humanize: bool | None = None, job_id: str = "",
) -> list[dict[str, Any]]:
    ban_keywords = ban_keywords if ban_keywords is not None else _ban_keywords()
    raw_texts: list[str] = []

    hook = _sanitize(package.get("hook"), ban_keywords)
    if hook:
        raw_texts.append(hook)

    for point in package.get("key_points") or ():
        text = _sanitize((point or {}).get("text"), ban_keywords)
        if text:
            raw_texts.append(text)

    # package["cta"] is read for nothing on purpose: the CTA scene and its
    # next-episode tease are a Shorts outro and stay off the thread (see
    # module docstring). The recap tweet below closes it instead.

    title = _fallback_title(package, ban_keywords)
    summary_lead = _pick_summary_lead(package, job_id, ban_keywords)
    summary_source: list[str] | None = None

    humanize = humanize if humanize is not None else _humanize_enabled()
    if humanize and raw_texts:
        source = package.get("source") or {}
        topic = str(source.get("topic") or package.get("topic") or "").strip()
        rewritten = _humanize_with_claude(
            raw_texts, topic=topic, title=str(source.get("title") or "").strip(),
        )
        if rewritten:
            filtered = [_sanitize(text, ban_keywords) for text in rewritten["texts"]]
            # Only adopt the rewrite if it didn't collapse a sentence to
            # empty -- a partial ban-keyword hit here means the rewrite
            # drifted into risky phrasing the original text never had, so
            # it's safer to keep the pre-rewrite text than to drop a tweet.
            # The title/lead/summary ride on that same verdict: they came
            # from the one call, so a body we distrust taints them too.
            if all(filtered):
                raw_texts = filtered
                title = _sanitize(rewritten["title"], ban_keywords, X_TITLE_MAX_CHARS) or title
                summary_lead = _sanitize(
                    rewritten["summary_lead"], ban_keywords, SUMMARY_LEAD_MAX_CHARS,
                ) or summary_lead
                summary_source = rewritten["summary"] or None

    prefix_reserve = _PREFIX_RESERVE_WIDTH if number_prefix else 0
    lead_reserve = len(title) + len(TITLE_SEPARATOR) if title else 0
    groups = _pack_sentences(
        raw_texts, max_chars=TWEET_MAX_CHARS, prefix_reserve=prefix_reserve,
        lead_reserve=lead_reserve,
    )

    # Title only rides along when there is a lead tweet to attach it to --
    # an empty package must stay empty rather than become a title-only post.
    if groups and title:
        groups[0] = f"{title}{TITLE_SEPARATOR}{groups[0]}"

    # The recap gets its own tweet rather than being packed in with body
    # sentences: it is a distinct block the reader should land on, which is
    # the whole point of closing with it. Same emptiness rule as the title.
    summary_tweet = _render_summary_tweet(
        summary_lead, _summary_lines(package, ban_keywords, summary_source),
        prefix_reserve=prefix_reserve,
    )
    if groups and summary_tweet:
        groups.append(summary_tweet)

    tweets: list[dict[str, Any]] = []
    total = len(groups)
    for i, text in enumerate(groups, start=1):
        prefix = f"{i}/{total} " if number_prefix else ""
        tweets.append({"index": i, "text": f"{prefix}{text}".strip()})
    return tweets


def render_text(payload: dict[str, Any]) -> str:
    total = len(payload["tweets"])
    return "\n\n".join(f"[{t['index']}/{total}] {t['text']}" for t in payload["tweets"])


def load_x_thread(job_dir: str | Path) -> dict[str, Any] | None:
    """Read an already-built x_thread.json without rebuilding it -- for a
    caller (e.g. the final_confirm gate) that wants to show a draft built
    earlier in the pipeline rather than pay for another humanize call."""
    try:
        return json.loads((Path(job_dir) / "x_thread.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def save_x_thread(job_dir: str | Path, payload: dict[str, Any]) -> None:
    """Persist an updated payload (e.g. after a bot records that the sources
    DM went out). Shared so callers don't each re-implement the write."""
    (Path(job_dir) / "x_thread.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


def build_x_thread(
    job_dir: str | Path, *, number_prefix: bool = False, humanize: bool | None = None,
) -> dict[str, Any] | None:
    """Build/refresh x_thread.json (+ .txt) for one job directory.

    Returns None if content_package.json doesn't exist yet (Stage 2 hasn't
    finished, or content_package build failed) -- the caller should tell the
    operator to run the script stage first rather than guessing at a thread.

    Refuses to rebuild (returns the existing payload unchanged) once the
    thread is marked posted: overwriting here would wipe tweet_ids/posted_at
    and make x_poster think a live thread was never posted, risking a
    duplicate. A real re-post after an intentional content change means
    removing x_thread.json (or its "posted" flag) by hand first.
    """
    job_dir = Path(job_dir)
    existing = load_x_thread(job_dir)
    if existing and existing.get("posted"):
        return existing

    package = content_package.load_content_package(job_dir)
    if package is None:
        return None

    ban_keywords = _ban_keywords()
    tweets = build_tweets(
        package, number_prefix=number_prefix, humanize=humanize, ban_keywords=ban_keywords,
        job_id=str(package.get("job_id") or job_dir.name),
    )
    photo_path = x_photo_card.build_thread_photo(package, job_dir)
    payload = {
        "job_id": package.get("job_id") or job_dir.name,
        "tweets": tweets,
        "char_counts": [len(t["text"]) for t in tweets],
        # Sources live beside the thread, not inside it: the bots DM this
        # text so the operator can copy-paste it wherever it belongs.
        "sources_text": build_sources_text(
            package, _load_pubmed_citations(job_dir), ban_keywords,
        ),
        "photo_path": str(photo_path) if photo_path else None,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "rule_v3",
    }
    save_x_thread(job_dir, payload)
    (job_dir / "x_thread.txt").write_text(render_text(payload) + "\n", encoding="utf-8")
    content_package.update_platform_flag(job_dir, "x_thread", True)
    return payload


def resolve_work_dir(job_id: str) -> Path:
    base = os.environ.get("WORK_DIR_BASE")
    if base:
        return Path(base) / job_id
    dev_dir = Path(__file__).resolve().parents[3]  # .../dev/src/common/adapters -> dev
    return dev_dir / "data" / "work" / job_id


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="content_package.json -> X 스레드 초안 (규칙 기반, Phase 4)")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--print", dest="print_output", action="store_true", help="생성 결과를 표준출력에도 출력")
    parser.add_argument("--number-prefix", action="store_true", help="트윗 앞에 1/ 2/ 형식 번호를 붙임")
    parser.add_argument(
        "--no-humanize", dest="no_humanize", action="store_true",
        help="Claude 캐주얼 리라이트 단계를 끄고 규칙 기반 원문 그대로 사용",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    job_dir = resolve_work_dir(args.job_id)
    humanize = False if args.no_humanize else None
    payload = build_x_thread(job_dir, number_prefix=args.number_prefix, humanize=humanize)
    if payload is None:
        print(f"content_package.json이 없습니다: {job_dir}. 먼저 스크립트 생성을 완료하세요.", file=sys.stderr)
        return 1
    if payload.get("posted"):
        print(
            f"이미 게시된 스레드라 다시 만들지 않았습니다: {job_dir / 'x_thread.json'} "
            f"({len(payload['tweets'])}개 트윗, posted_at={payload.get('posted_at')})"
        )
    else:
        print(f"x_thread.json 작성 완료: {job_dir / 'x_thread.json'} ({len(payload['tweets'])}개 트윗)")
    if args.print_output:
        print()
        print(render_text(payload))
        if payload.get("sources_text"):
            print()
            print("--- 출처 (스레드에는 넣지 않음, 슬랙 DM용) ---")
            print(payload["sources_text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
