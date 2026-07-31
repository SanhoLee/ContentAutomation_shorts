"""
trend_probe.py — what are viewers actually searching for, in which language?

Research sources get English only (see evidence_probe). Trend sources are the
opposite case: people search in their own language, so the signal is genuinely
multilingual. The ladder runs `en` → `ja` → `ko` and **stops at the first
language that passes the quality gate**, so the common case costs one request.
Japanese sits in the middle on merit — an older-skewing market where brain-health
content is mature, which matches a 50+ channel.

The gate exists because autocomplete drifts. `trend_observations` already holds
"기차의 꿈", "꿈돌이", "꿈빛파티시엘" — the seed "꿈" pulled in an anime, a mascot
and a film. Measured against live suggest:

    꿈        10% domain match  → drift, discarded
    치매 예방  100%              → kept
    기억력     100%              → kept

The vocabulary doing the filtering is not hardcoded: it is the channel's own
`keywords` table unioned with research_categories.json, ~1,466 terms today, and
it grows every time the channel publishes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

SUGGEST_URL = "https://suggestqueries.google.com/complete/search"

# en first, then ja, then ko. Each entry is (lang, hl, gl).
LANGUAGE_LADDER = (("en", "en", "US"), ("ja", "ja", "JP"), ("ko", "ko", "KR"))

# Share of suggestions that must match the channel vocabulary for a language's
# results to be trusted. 0.5 sits between the measured 10% (drift) and 100%.
MIN_DOMAIN_MATCH = 0.5
# Below this many surviving keywords the sample is too thin to judge, so the
# ladder advances even if the ratio looks fine.
MIN_KEPT = 3

# Commercial/spam suggestions carry demand but not content. The Korean half is
# the list already used by 0_script.assess_pubmed_query; en/ja are the same idea.
COMMERCIAL = re.compile(
    r"추천|가격|순위|고르는\s*법|브랜드|후기|먹는\s*법|영양제|최저가|쿠팡|직구"
    r"|price|cheap|buy|coupon|amazon|supplement|recipe|\.com|\.co\.kr|pdf|download"
    r"|サプリ|通販|価格|おすすめ|激安|口コミ|楽天",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TrendResult:
    seed: str
    # The locale that passed, not the language of the keywords: Google answers
    # in the seed's own language, so an `en` locale on a Korean seed still
    # returns Korean suggestions. What matters is which rung stopped the ladder.
    language: str
    keywords: tuple[str, ...] = ()
    status: str = "ok"
    domain_match: float = 0.0
    dropped_commercial: int = 0
    note: str = ""
    attempts: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


# ---------------------------------------------------------------------------
# Channel vocabulary
# ---------------------------------------------------------------------------

def build_channel_vocabulary(
    conn=None,
    categories_path: str | None = None,
    min_length: int = 2,
) -> frozenset[str]:
    """Terms the channel actually uses: published title/tag keywords plus the
    research category keywords. Lowercased, short tokens dropped.

    Never raises — a missing table or file just yields a smaller vocabulary, and
    an empty one disables the gate rather than rejecting everything.
    """
    vocabulary: set[str] = set()
    if conn is not None:
        try:
            for (keyword,) in conn.execute("SELECT DISTINCT keyword FROM keywords"):
                text = str(keyword or "").strip().lower()
                if len(text) >= min_length:
                    vocabulary.add(text)
        except Exception:
            pass

    from pathlib import Path
    path = Path(categories_path) if categories_path else (
        Path(__file__).resolve().parent / "research_categories.json"
    )
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        for entry in raw.get("categories") or ():
            for keyword in entry.get("keywords") or ():
                text = str(keyword).strip().lower()
                if len(text) >= min_length:
                    vocabulary.add(text)
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return frozenset(vocabulary)


def domain_match_ratio(keywords: Sequence[str], vocabulary: Iterable[str]) -> float:
    """Share of suggestions containing at least one channel term."""
    terms = tuple(vocabulary)
    if not keywords or not terms:
        return 1.0 if not terms else 0.0
    matched = sum(1 for k in keywords if any(t in k.lower() for t in terms))
    return matched / len(keywords)


# ---------------------------------------------------------------------------
# Suggest
# ---------------------------------------------------------------------------

def _default_fetch(url: str, params: dict[str, str]) -> Any:
    try:
        import requests
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        text = response.text
    except ModuleNotFoundError:
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen
        request = Request(f"{url}?{urlencode(params)}", headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=10) as handle:
            text = handle.read().decode("utf-8", errors="replace")
    text = text.strip()
    if text.startswith(")]}'"):
        text = text.split("\n", 1)[1]
    return json.loads(text)


def fetch_suggestions(
    seed: str,
    hl: str,
    gl: str,
    youtube: bool = False,
    fetch: Callable[..., Any] | None = None,
) -> list[str]:
    """Autocomplete for one language.

    `oe=utf-8` is not optional: without it Korean comes back mojibake
    (`ġ�� ����`) and Japanese fails to parse outright. The old call site omitted
    it and decoded with errors="replace", so the corruption looked like data.
    """
    fetch = fetch or _default_fetch
    params = {"client": "firefox", "hl": hl, "gl": gl, "ie": "utf-8", "oe": "utf-8", "q": seed}
    if youtube:
        params["ds"] = "yt"
    payload = fetch(SUGGEST_URL, params)
    values = payload[1] if isinstance(payload, list) and len(payload) > 1 else []
    return [" ".join(str(v).split()).strip() for v in values if str(v).strip()]


def probe(
    seed: str,
    vocabulary: Iterable[str],
    languages: Sequence[tuple[str, str, str]] = LANGUAGE_LADDER,
    include_youtube: bool = True,
    fetch: Callable[..., Any] | None = None,
) -> TrendResult:
    """Walk the language ladder, stopping at the first language that passes.

    A language fails when its suggestions drift off-topic — that is the "꿈 →
    꿈돌이" case — and the next language gets a turn. If none pass, the caller
    learns the seed itself is too ambiguous to mine for trends.
    """
    vocabulary = tuple(vocabulary)
    attempts: list[dict[str, Any]] = []

    for lang, hl, gl in languages:
        raw: list[str] = []
        try:
            raw.extend(fetch_suggestions(seed, hl, gl, youtube=False, fetch=fetch))
            if include_youtube:
                raw.extend(fetch_suggestions(seed, hl, gl, youtube=True, fetch=fetch))
        except Exception as exc:
            attempts.append({"language": lang, "error": type(exc).__name__})
            continue

        deduped = list(dict.fromkeys(raw))
        kept = [k for k in deduped if not COMMERCIAL.search(k)]
        dropped = len(deduped) - len(kept)
        ratio = domain_match_ratio(kept, vocabulary)
        on_topic = [k for k in kept if any(t in k.lower() for t in vocabulary)] if vocabulary else kept

        passed = ratio >= MIN_DOMAIN_MATCH and len(on_topic) >= MIN_KEPT
        attempts.append({"language": lang, "fetched": len(deduped), "kept": len(kept),
                         "commercial_dropped": dropped, "domain_match": round(ratio, 3),
                         "passed": passed})
        if passed:
            return TrendResult(
                seed=seed, language=lang, keywords=tuple(on_topic), status="ok",
                domain_match=round(ratio, 3), dropped_commercial=dropped,
                attempts=tuple(attempts),
            )

    return TrendResult(
        seed=seed, language="", status="off_topic",
        note="어느 언어에서도 채널 주제와 맞는 검색어를 찾지 못했습니다. 시드가 모호할 수 있습니다.",
        attempts=tuple(attempts),
    )
