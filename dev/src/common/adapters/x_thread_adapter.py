#!/usr/bin/env python3
"""x_thread_adapter.py — content_package.json -> X(Twitter) thread draft.

Phase 4 of the Pareto design spec. Every tweet's raw text still comes from
a field content_package.py already computed. On top of that, one bounded,
non-retrying Claude Haiku call (see _humanize_texts_with_claude) rewrites
the batch into a more casual, non-expert tone before truncation/hashtags
are applied -- X's audience (20s-50s) is far broader than the 50+ YouTube
viewer, and script prose written for narration reads as stiff/robotic in a
tweet. If ANTHROPIC_API_KEY is unset, the budget guard trips, or the call
fails for any reason, this falls back silently to the original rule-based
text (production continuity over a downstream-only artifact). Posting is
out of scope here — this only writes a draft an operator copies by hand
(or a future phase wires to the X API).

Source order is hook, then each key_point, then cta.action (+
next_topic_tease as a question). These sentences are packed greedily into
as few tweets as fit under TWEET_MAX_CHARS (139 -- X weighs each CJK
character as 2 toward its 280-weighted-character cap, so pure-Korean text
tops out around 140, not the ~270 an ASCII tweet gets) rather than one
sentence per tweet, since most individual sentences here run far shorter
than the limit. Hashtags are appended only to the last tweet.

Sentences that trip the Phase 1 safety ban-list (topic_score's
ban_keywords — "완치", "보장", "기적", ...) are dropped rather than posted
with a softened claim, since a dropped sentence is trivially safe and a
softened one still risks an implied medical guarantee. The filter runs
again after the humanize rewrite, since a rewrite could reintroduce risky
phrasing that wasn't in the original.
"""

from __future__ import annotations

import argparse
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

# X weighs every CJK character as 2 toward its 280-weighted-character cap,
# so a pure-Korean post tops out around 280/2=140 chars, not the ~270-280
# an ASCII tweet gets. 139 leaves a 1-char margin.
TWEET_MAX_CHARS = 139
MAX_HASHTAGS_LAST_TWEET = 2
MAX_SOURCE_LINKS = 3
# X's t.co wrapper counts every link as this many characters regardless of
# its real length. Used only to decide how many source lines fit; the raw
# TWEET_MAX_CHARS budget above still applies as the outer safety net.
LINK_COST_CHARS = 23
SOURCES_LABEL = "출처"

X_THREAD_HUMANIZE_MODEL = os.environ.get("X_THREAD_HUMANIZE_MODEL", "claude-haiku-4-5-20251001")
X_THREAD_HUMANIZE_TIMEOUT_SEC = int(os.environ.get("X_THREAD_HUMANIZE_TIMEOUT_SEC", "20"))
X_THREAD_HUMANIZE_MAX_TOKENS = 1024
FALSE_VALUES = {"0", "false", "off", "no"}

SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
JSON_ARRAY = re.compile(r"\[.*\]", re.S)
URL_PATTERN = re.compile(r"https?://\S+")

HUMANIZE_PROMPT = """다음은 유튜브 쇼츠 대본에서 뽑은 문장들이다. 이 문장들을 X(트위터) 스레드용으로 다시 써라.
주제: {topic}

조건:
- 전문가/방송 멘트처럼 딱딱하지 않게, 20~50대가 평소 트위터에 편하게 쓰는 말투로 바꾼다.
- "~습니다" 같은 격식체 대신 반말 섞인 구어체나 편한 해요체를 쓴다. 완벽한 문장보다 사람이 쓴 것처럼 자연스러운 게 우선이다.
- 과장, 의학적 단정("완치", "보장", "기적" 등)은 절대 쓰지 않는다.
- 각 문장의 핵심 정보와 순서, 문장 개수를 그대로 유지한다. 문장을 합치거나 나누지 않는다.
- 이모지나 해시태그는 넣지 않는다 (별도로 붙는다).

문장 목록:
{numbered}

출력은 순수 JSON 배열만. 예: ["문장1", "문장2"]. 다른 설명은 절대 붙이지 않는다."""


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


def _humanize_texts_with_claude(raw_texts: list[str], *, topic: str) -> list[str] | None:
    """One bounded, non-retrying rewrite pass. Returns None on any failure
    so the caller can fall back to the original rule-based text rather than
    blocking a downstream-only artifact on a Claude outage."""
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
    payload = {
        "model": X_THREAD_HUMANIZE_MODEL,
        "max_tokens": X_THREAD_HUMANIZE_MAX_TOKENS,
        "messages": [{"role": "user", "content": HUMANIZE_PROMPT.format(topic=topic or "뇌 건강", numbered=numbered)}],
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

    try:
        claude_cost.record_usage("x_thread_humanize", X_THREAD_HUMANIZE_MODEL, data)
    except Exception:
        pass

    text = "".join(block.get("text", "") for block in data.get("content") or [] if block.get("type") == "text")
    match = JSON_ARRAY.search(text)
    if not match:
        return None
    try:
        rewritten = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(rewritten, list) or len(rewritten) != len(raw_texts):
        return None
    return [str(t).strip() for t in rewritten]


def _apply_safety_filter(text: str, ban_keywords: list[str]) -> str:
    """Drop a sentence outright if it trips a ban keyword -- no softened
    rewrite, since that still risks an implied guarantee slipping through."""
    if not text:
        return ""
    lowered = text.lower()
    if any(bad.lower() in lowered for bad in ban_keywords if bad):
        return ""
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


def _pack_sentences(texts: list[str], *, max_chars: int, prefix_reserve: int) -> list[str]:
    """Greedily fill each tweet with as many whole sentences as fit,
    instead of one sentence per tweet -- a 70-char sentence alone in a
    139-char tweet wastes half the post. A single sentence longer than
    the budget is truncated at a boundary rather than left oversized."""
    budget = max(1, max_chars - prefix_reserve)
    groups: list[str] = []
    current = ""
    for text in texts:
        text = text.strip()
        if not text:
            continue
        candidate = f"{current} {text}".strip() if current else text
        if len(candidate) <= budget:
            current = candidate
            continue
        if current:
            groups.append(current)
        current = _truncate_at_boundary(text, budget) if len(text) > budget else text
    if current:
        groups.append(current)
    return groups


def _pubmed_url(pmid: str) -> str:
    return f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"


def _link_aware_length(line: str) -> int:
    """Line length with any embedded URL discounted to t.co's fixed cost."""
    match = URL_PATTERN.search(line)
    if not match:
        return len(line)
    literal = match.group(0)
    return len(line) - len(literal) + LINK_COST_CHARS


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


def _build_sources_text(
    package: dict[str, Any],
    pubmed_citations: list[dict[str, Any]] | None,
    ban_keywords: list[str],
) -> str:
    """PMID links when available (real and verifiable), else evidence's
    source_hint text. Returns "" when neither exists, so the caller skips
    the sources tweet rather than posting an empty reference."""
    lines = _source_lines_from_citations(pubmed_citations or [], ban_keywords)
    if not lines:
        lines = _source_lines_from_evidence(package.get("evidence") or [], ban_keywords)
    if not lines:
        return ""
    budget = TWEET_MAX_CHARS - len(SOURCES_LABEL) - 1
    while lines:
        used = sum(_link_aware_length(line) + 1 for line in lines) - 1
        if used <= budget:
            break
        lines.pop()
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


def build_tweets(
    package: dict[str, Any], *, number_prefix: bool = False, ban_keywords: list[str] | None = None,
    humanize: bool | None = None, pubmed_citations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    ban_keywords = ban_keywords if ban_keywords is not None else _ban_keywords()
    raw_texts: list[str] = []

    hook = _apply_safety_filter(str(package.get("hook") or "").strip(), ban_keywords)
    if hook:
        raw_texts.append(hook)

    for point in package.get("key_points") or ():
        text = _apply_safety_filter(str((point or {}).get("text") or "").strip(), ban_keywords)
        if text:
            raw_texts.append(text)

    cta = package.get("cta") or {}
    action = str(cta.get("action") or "").strip()
    tease = str(cta.get("next_topic_tease") or "").strip()
    closing_parts = [p for p in (action, f"다음 편에서는 {tease} 이야기, 궁금하지 않으세요?" if tease else "") if p]
    closing = _apply_safety_filter(" ".join(closing_parts).strip(), ban_keywords)
    if closing:
        raw_texts.append(closing)

    humanize = humanize if humanize is not None else _humanize_enabled()
    if humanize and raw_texts:
        topic = str((package.get("source") or {}).get("topic") or package.get("topic") or "").strip()
        rewritten = _humanize_texts_with_claude(raw_texts, topic=topic)
        if rewritten:
            filtered = [_apply_safety_filter(text, ban_keywords) for text in rewritten]
            # Only adopt the rewrite if it didn't collapse a sentence to
            # empty -- a partial ban-keyword hit here means the rewrite
            # drifted into risky phrasing the original text never had, so
            # it's safer to keep the pre-rewrite text than to drop a tweet.
            if all(filtered):
                raw_texts = filtered

    hashtags = [tag for tag in (package.get("hashtags") or [])][:MAX_HASHTAGS_LAST_TWEET]

    prefix_reserve = _PREFIX_RESERVE_WIDTH if number_prefix else 0
    groups = _pack_sentences(raw_texts, max_chars=TWEET_MAX_CHARS, prefix_reserve=prefix_reserve)

    if groups and hashtags:
        suffix = f" {' '.join(hashtags)}"
        budget = max(1, TWEET_MAX_CHARS - prefix_reserve - len(suffix))
        last = groups[-1]
        if len(last) > budget:
            last = _truncate_at_boundary(last, budget)
        groups[-1] = f"{last}{suffix}".strip()

    # Appended after hashtags on purpose: the sources tweet is citations, not
    # copy, so it stays out of the humanize rewrite above and never becomes
    # the tweet that carries the hashtags.
    sources_text = _build_sources_text(package, pubmed_citations, ban_keywords)
    if groups and sources_text:
        groups.append(sources_text)

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

    tweets = build_tweets(
        package, number_prefix=number_prefix, humanize=humanize,
        pubmed_citations=_load_pubmed_citations(job_dir),
    )
    photo_path = x_photo_card.build_thread_photo(package, job_dir)
    payload = {
        "job_id": package.get("job_id") or job_dir.name,
        "tweets": tweets,
        "char_counts": [len(t["text"]) for t in tweets],
        "photo_path": str(photo_path) if photo_path else None,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": "rule_v1",
    }
    (job_dir / "x_thread.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
