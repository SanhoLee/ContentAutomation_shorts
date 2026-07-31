"""
evidence_probe.py — does published research actually back this topic?

Job `goal_20260731_113831_929708_24cc6352` shipped without evidence because
`0_script.py` ran one PubMed query, got zero hits, and gave up. The literature
existed: dropping a single modifier took `uncorrected refractive error dementia
risk` (0 hits) to `refractive error dementia` (30). This module is the retry
that was missing.

Two invariants, both learned from real failures:

  English only.  Korean queries match nothing, and worse, PubMed silently keeps
  whatever Latin token it can find and discards the rest. Job
  `auto_20260720_222619_877036_949d0511` sent "중년 고LDL 콜레스테롤, 뇌혈관 건강,
  치매 위험" after a Claude budget stop killed translation; PubMed reduced it to
  `"ldl"[All Fields]`, matched 134,883 papers, and the top 3 by relevance — a
  forest-regeneration travelogue among them — became the script's "evidence",
  recorded as status ok. Every rung here refuses to send Hangul.

  No Claude calls.  Translation costs a Claude request; a ladder that re-invoked
  it per rung would multiply that cost. Rungs 2+ are pure string work, and the
  final rung reuses the vetted queries already sitting in
  research_categories.json. Network calls to Europe PMC / NCBI are free.
"""

from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Sequence

EUROPE_PMC_SEARCH = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
PUBMED_ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"

# A query matching this many papers has lost its specificity: the "top N by
# relevance" are no longer about our topic. The two sources need different
# ceilings because Europe PMC indexes far more than PubMed — "hearing loss AND
# dementia risk" returns 720 there and 20,441 here.
#
# Measured, so the ceilings sit in the gap rather than on a guess:
#   PubMed      valid 720..11,421      collapsed "LDL" 134,890
#   Europe PMC  valid 1,295..128,796   collapsed "LDL" 365,469, "health" 12.1M
MAX_PLAUSIBLE_HITS = 50_000
MAX_PLAUSIBLE_HITS_EPMC = 200_000
# Below this, there is not enough to build a claim on, so try the next rung.
MIN_USEFUL_HITS = 10
# How many records to pull for the citation/recency estimate. One call gives
# hitCount, citedByCount and pubYear together, so a candidate costs one request.
SAMPLE_SIZE = 25
RECENT_YEARS = 5

_HANGUL = re.compile(r"[가-힣]")
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9-]+")

# Terms that narrow a query without carrying the claim. Dropped first.
MODIFIER_TERMS = frozenset({
    "uncorrected", "corrected", "risk", "risks", "effect", "effects", "impact",
    "association", "associations", "relationship", "correlation", "study",
    "studies", "trial", "review", "analysis", "patients", "people", "adults",
    "older", "elderly", "aging", "aged", "chronic", "acute", "severe", "mild",
    "moderate", "early", "late", "daily", "long", "term", "middle", "age",
    "related", "induced", "based", "level", "levels", "factor", "factors",
    "management", "prevention", "screening", "intervention", "outcome",
    "outcomes", "potential", "possible", "common", "new", "novel",
})

# The claim itself. Never dropped — without these the query stops being ours.
OUTCOME_TERMS = frozenset({
    "dementia", "alzheimer", "alzheimers", "cognitive", "cognition", "memory",
    "brain", "neurodegeneration", "hippocampus", "hippocampal", "stroke",
    "mci", "amyloid", "tau", "neuroplasticity", "neuroinflammation",
})

STATUS_OK = "ok"
STATUS_NO_RESULTS = "no_results"
STATUS_NO_ENGLISH_QUERY = "no_english_query"


def has_hangul(text: str) -> bool:
    return bool(_HANGUL.search(text or ""))


@dataclass(frozen=True)
class EvidenceResult:
    """What the ladder found, and which rung found it."""
    topic: str
    resolved_query: str
    ladder_rung: str
    status: str
    hit_count: int = 0
    recent_count: int = 0
    median_citations: float = 0.0
    source: str = "europe_pmc"
    note: str = ""
    attempts: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def recent_ratio(self) -> float:
        return (self.recent_count / self.hit_count) if self.hit_count else 0.0


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------

def _tokens(query: str) -> list[str]:
    return _TOKEN.findall(query or "")


def narrow_query(query: str) -> str:
    """Drop modifier terms, keep the claim. The step that was missing."""
    kept = [t for t in _tokens(query) if t.lower() not in MODIFIER_TERMS]
    return " ".join(kept)


def core_query(query: str) -> str:
    """Last resort before the category fallback: outcome terms plus the single
    most specific remaining word (longest wins — 'refractive' over 'error')."""
    tokens = _tokens(query)
    outcomes = [t for t in tokens if t.lower() in OUTCOME_TERMS]
    rest = [t for t in tokens if t.lower() not in OUTCOME_TERMS and t.lower() not in MODIFIER_TERMS]
    if not outcomes:
        return " ".join(rest[:2])
    longest = max(rest, key=len) if rest else ""
    return " ".join([longest, *outcomes[:1]]).strip()


def build_ladder(english_query: str, category_query: str | None = None) -> list[tuple[str, str]]:
    """(rung_name, query) pairs, most specific first, Hangul removed entirely.

    Duplicate and empty rungs are dropped so a short query does not spend three
    identical requests.
    """
    rungs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, candidate in (
        ("full", english_query),
        ("narrowed", narrow_query(english_query)),
        ("core", core_query(english_query)),
        ("category", category_query or ""),
    ):
        candidate = " ".join((candidate or "").split()).strip()
        # The invariant: a Korean query is never sent, not even as a last resort.
        if not candidate or has_hangul(candidate):
            continue
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        rungs.append((name, candidate))
    return rungs


# ---------------------------------------------------------------------------
# Europe PMC
# ---------------------------------------------------------------------------

def _default_fetch(url: str, params: dict[str, str]) -> Any:
    """Kept local so this module works from planner and script stages alike."""
    try:
        import requests
        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()
        return json.loads(response.text)
    except ModuleNotFoundError:
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen
        request = Request(f"{url}?{urlencode(params)}", headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=20) as handle:
            return json.loads(handle.read().decode("utf-8", errors="replace"))


def search_europe_pmc(query: str, fetch: Callable[..., Any] | None = None) -> dict[str, Any]:
    """hitCount plus a sample carrying citedByCount and pubYear — one request.

    Sorting by citations alone would surface topic-irrelevant mega-reviews (a
    hearing/dementia query returns "Heart Disease and Stroke Statistics-2023"
    first), so the sample stays in relevance order and citations are read off it.
    """
    fetch = fetch or _default_fetch
    payload = fetch(EUROPE_PMC_SEARCH, {
        "query": query,
        "format": "json",
        "pageSize": str(SAMPLE_SIZE),
        "resultType": "core",
    })
    results = ((payload or {}).get("resultList") or {}).get("result") or []
    cutoff = date.today().year - RECENT_YEARS
    years, citations = [], []
    for item in results:
        try:
            years.append(int(item.get("pubYear")))
        except (TypeError, ValueError):
            pass
        try:
            citations.append(int(item.get("citedByCount") or 0))
        except (TypeError, ValueError):
            pass
    hit_count = int((payload or {}).get("hitCount") or 0)
    recent_share = (sum(1 for y in years if y >= cutoff) / len(years)) if years else 0.0
    return {
        "hit_count": hit_count,
        # hitCount covers all years; the sample tells us how much of it is recent.
        "recent_count": int(round(hit_count * recent_share)),
        "median_citations": float(statistics.median(citations)) if citations else 0.0,
        "sample_size": len(results),
    }


# ---------------------------------------------------------------------------
# PubMed
# ---------------------------------------------------------------------------

def search_pubmed(
    query: str,
    retmax: int = 3,
    api_key: str = "",
    fetch: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """PMIDs for the abstract fetch, plus what PubMed made of our query.

    Europe PMC and PubMed disagree on purpose: Europe PMC is broader (the
    glasses query returns 187 there and 0 here), so candidate scoring uses that
    while the script stage — which needs real abstracts — walks this ladder.
    """
    fetch = fetch or _default_fetch
    params = {"db": "pubmed", "term": query, "retmax": str(retmax),
              "sort": "relevance", "retmode": "json"}
    if api_key:
        params["api_key"] = api_key
    payload = fetch(PUBMED_ESEARCH, params)
    result = (payload or {}).get("esearchresult") or {}
    return {
        "hit_count": int(result.get("count") or 0),
        "pmids": list(result.get("idlist") or []),
        # PubMed echoes how it parsed the query. When Hangul slipped through,
        # this is where "ldl"[All Fields] alone showed up.
        "translation": str(result.get("querytranslation") or ""),
    }


def query_survival(query: str, translation: str) -> float:
    """Share of our meaningful terms PubMed actually kept.

    The English-only invariant already blocks the Hangul collapse, so this
    guards the subtler case: an English query carrying a term PubMed does not
    index, silently answering a broader question than we asked.
    """
    terms = [t.lower() for t in _tokens(query) if t.lower() not in MODIFIER_TERMS]
    if not terms:
        return 1.0
    lowered = (translation or "").lower()
    return sum(1 for t in terms if t in lowered) / len(terms)


def probe_pubmed(
    topic: str,
    english_query: str,
    category_query: str | None = None,
    retmax: int = 3,
    api_key: str = "",
    fetch: Callable[..., Any] | None = None,
) -> EvidenceResult:
    """Same ladder, but returns PMIDs so the script stage can fetch abstracts."""
    ladder = build_ladder(english_query, category_query)
    if not ladder:
        return EvidenceResult(
            topic=topic, resolved_query="", ladder_rung="none",
            status=STATUS_NO_ENGLISH_QUERY, source="pubmed",
            note="영어 검색어를 만들지 못했습니다. 카테고리 기본 검색어도 없습니다.",
        )

    attempts: list[dict[str, Any]] = []
    for rung, query in ladder:
        try:
            found = search_pubmed(query, retmax, api_key, fetch)
        except Exception as exc:
            attempts.append({"rung": rung, "query": query, "error": type(exc).__name__})
            continue
        hits = found["hit_count"]
        survival = query_survival(query, found["translation"])
        if hits > MAX_PLAUSIBLE_HITS:
            reason = f"too_broad({hits})"
        elif not found["pmids"]:
            reason = "no_hits"
        elif survival < 0.5:
            reason = f"query_mangled({survival:.0%})"
        else:
            reason = ""
        attempts.append({"rung": rung, "query": query, "hits": hits,
                         "accepted": not reason, "reason": reason})
        if not reason:
            return EvidenceResult(
                topic=topic, resolved_query=query, ladder_rung=rung, status=STATUS_OK,
                hit_count=hits, source="pubmed", attempts=tuple(attempts),
                note=",".join(found["pmids"]),
            )

    return EvidenceResult(
        topic=topic, resolved_query=ladder[-1][1], ladder_rung="exhausted",
        status=STATUS_NO_RESULTS, source="pubmed", attempts=tuple(attempts),
        note="검색어를 넓혀가며 모두 시도했지만 PubMed 초록을 찾지 못했습니다.",
    )


def _judge(stats: dict[str, Any]) -> tuple[bool, str]:
    """Accept, or say why this rung is not trustworthy."""
    hits = stats["hit_count"]
    if hits > MAX_PLAUSIBLE_HITS_EPMC:
        return False, f"too_broad({hits})"
    if hits < MIN_USEFUL_HITS:
        return False, f"too_few({hits})"
    return True, ""


def probe(
    topic: str,
    english_query: str,
    category_query: str | None = None,
    fetch: Callable[..., Any] | None = None,
) -> EvidenceResult:
    """Walk the ladder, stopping at the first rung that holds up.

    `english_query` is whatever translation produced. If it is empty or Korean —
    the budget-stop case — the ladder simply starts at the category query.
    """
    ladder = build_ladder(english_query, category_query)
    if not ladder:
        return EvidenceResult(
            topic=topic, resolved_query="", ladder_rung="none",
            status=STATUS_NO_ENGLISH_QUERY,
            note="영어 검색어를 만들지 못했습니다. 카테고리 기본 검색어도 없습니다.",
        )

    attempts: list[dict[str, Any]] = []
    for rung, query in ladder:
        try:
            stats = search_europe_pmc(query, fetch)
        except Exception as exc:  # network flake must not kill the pipeline
            attempts.append({"rung": rung, "query": query, "error": type(exc).__name__})
            continue
        accepted, reason = _judge(stats)
        attempts.append({"rung": rung, "query": query, "hits": stats["hit_count"],
                         "accepted": accepted, "reason": reason})
        if accepted:
            return EvidenceResult(
                topic=topic, resolved_query=query, ladder_rung=rung, status=STATUS_OK,
                hit_count=stats["hit_count"], recent_count=stats["recent_count"],
                median_citations=stats["median_citations"],
                attempts=tuple(attempts),
            )

    return EvidenceResult(
        topic=topic, resolved_query=ladder[-1][1], ladder_rung="exhausted",
        status=STATUS_NO_RESULTS,
        note="검색어를 넓혀가며 모두 시도했지만 쓸 만한 논문을 찾지 못했습니다.",
        attempts=tuple(attempts),
    )


# ---------------------------------------------------------------------------
# Candidate scoring
# ---------------------------------------------------------------------------

def _log_scale(value: float, ceiling: float) -> float:
    """Compress counts so one huge number cannot dominate a 0..1 metric."""
    import math
    if value <= 0:
        return 0.0
    return min(1.0, math.log10(1.0 + value) / math.log10(1.0 + ceiling))


def evidence_metrics(result: EvidenceResult) -> dict[str, float]:
    """0..1 signals for objective_planner's normalized_metrics.

    Topic-level, unlike research_signals.research_depth which resolves at
    category granularity and therefore rated the zero-paper glasses topic well.
    """
    if not result.ok:
        return {"evidence_volume": 0.0, "evidence_recency": 0.0, "evidence_citation": 0.0}
    return {
        "evidence_volume": round(_log_scale(result.hit_count, MAX_PLAUSIBLE_HITS_EPMC), 4),
        "evidence_recency": round(min(1.0, result.recent_ratio), 4),
        # Capped at 50 median citations: enough to tell live topics from dead
        # ones without letting a review article decide the score.
        "evidence_citation": round(_log_scale(result.median_citations, 50), 4),
    }


# ---------------------------------------------------------------------------
# Persistence — cache today, corpus over time
# ---------------------------------------------------------------------------

def record_observation(conn, result: EvidenceResult, observed: date | None = None) -> None:
    """Store what the probe found. Never raises: an unwritable log must not
    fail a job that already has its evidence."""
    if conn is None:
        return
    try:
        conn.execute(
            "INSERT OR REPLACE INTO evidence_observations"
            "(topic, query, resolved_query, ladder_rung, source, status,"
            " hit_count, recent_count, median_citations, observed_date)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (result.topic, result.resolved_query, result.resolved_query,
             result.ladder_rung, result.source, result.status,
             result.hit_count, result.recent_count, result.median_citations,
             (observed or date.today()).isoformat()),
        )
        conn.commit()
    except Exception:
        pass


def cached_observation(conn, topic: str, source: str = "europe_pmc",
                       observed: date | None = None) -> EvidenceResult | None:
    """Today's result for this topic, if we already asked. Keeps a re-planned
    job from re-probing the same eight candidates."""
    if conn is None:
        return None
    try:
        row = conn.execute(
            "SELECT resolved_query, ladder_rung, status, hit_count, recent_count,"
            " median_citations FROM evidence_observations"
            " WHERE topic=? AND source=? AND observed_date=?",
            (topic, source, (observed or date.today()).isoformat()),
        ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    return EvidenceResult(
        topic=topic, resolved_query=row[0], ladder_rung=row[1], status=row[2],
        hit_count=int(row[3] or 0), recent_count=int(row[4] or 0),
        median_citations=float(row[5] or 0.0), source=source, note="cached",
    )


def category_query_for(topic_family: str) -> str:
    """The vetted English query for a topic family, e.g. 'hearing loss AND
    dementia risk'. Comes from research_categories.json, so the ladder's last
    rung costs nothing and is guaranteed to be English.

    Returns "" when nothing matches, and that is deliberate: a topic with no
    category should fail honestly rather than borrow another category's papers.
    Filling an unmatched topic with generic brain-health literature is exactly
    the contamination this module exists to prevent.
    """
    try:
        from research_signals import load_category_signals, match_category
        signal = match_category(topic_family, load_category_signals())
    except Exception:
        return ""
    return str(getattr(signal, "query", "") or "") if signal else ""
