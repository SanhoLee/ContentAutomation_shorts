#!/usr/bin/env python3
"""topic_candidate_pipeline.py — orchestrates the Phase 1 seed-less topic funnel.

    [A] seed pool (topic_seed_pool)              -- no human seed required
    [B] batch trend_probe.probe() per seed        -- existing drift/commercial gate, unchanged
    [C] normalize suggest strings into candidates -- length filter, near-dup tagging
    [D] topic_score.score_candidate() + rank
    [E] write data/topics/{raw,eligible,rejected}/

No LLM calls anywhere in this path — everything here is rules, an API call to
Google/YouTube suggest, and SQLite reads.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable

import story_types
import topic_domain
import topic_score
import topic_seed_pool
import trend_probe

COMMON_DIR = Path(__file__).resolve().parent
DEV_DIR = COMMON_DIR.parents[1]
CONFIG_DIR = DEV_DIR / "config"
DATA_TOPICS_DIR = DEV_DIR / "data" / "topics"
PIPELINE_CONFIG_PATH = CONFIG_DIR / "topic_pipeline.json"

DEFAULT_PIPELINE_CONFIG: dict[str, Any] = {
    "max_seeds_total": 60,
    "max_seeds_per_category": 8,
    "min_seed_len": 2,
    "max_seed_len": 20,
    "suggest_sleep_ms": 200,
    "eligible_top_k": 15,
    "score_manual_topics": False,
    "use_google_trends_bonus": False,
    "extra_seeds": [],
    "auto_consume": False,
}

MIN_CANDIDATE_LEN = 4
MAX_CANDIDATE_LEN = 40
NEAR_DUPLICATE_SIMILARITY = 0.9

# A human's pick, recorded next to the queues it points into.
SELECTED_FILENAME = "selected.json"
STATUS_SELECTED = "selected"
DEFAULT_TOP_N = 3

SCORE_LABELS = {
    "domain_relevance": "뇌 관련성",
    "niche_relevance": "채널 적합",
    "search_intent_fit": "검색 의도",
    "evidence_potential": "근거 가능성",
    "novelty_vs_history": "신선도",
    "safety_tone": "안전 표현",
}

_FEEDBACK_SPEC = importlib.util.spec_from_file_location(
    "brain50_topic_feedback", COMMON_DIR.parent / "youtube" / "6_youtube_feedback.py"
)
if _FEEDBACK_SPEC is None or _FEEDBACK_SPEC.loader is None:
    raise RuntimeError("6_youtube_feedback.py를 로드할 수 없습니다.")
feedback = importlib.util.module_from_spec(_FEEDBACK_SPEC)
_FEEDBACK_SPEC.loader.exec_module(feedback)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_pipeline_config(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path else PIPELINE_CONFIG_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    return {**DEFAULT_PIPELINE_CONFIG, **raw}


def _default_conn():
    try:
        return feedback.connect()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# [C] Candidate normalize
# ---------------------------------------------------------------------------

def _slugify(text: str, max_len: int = 40) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w가-힣]+", "-", text)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return (text or "topic")[:max_len]


def _normalize_candidate(
    keyword: str,
    seed_item: topic_seed_pool.SeedItem,
    vocabulary: Iterable[str],
    probe_seed: str | None = None,
) -> dict[str, Any] | None:
    """`probe_seed` is what was actually sent to suggest, which differs from
    `seed_item.seed` on the bare-seed fallback path."""
    probe_seed = probe_seed or seed_item.seed
    text = " ".join(str(keyword).split()).strip()
    if not (MIN_CANDIDATE_LEN <= len(text) <= MAX_CANDIDATE_LEN):
        return None
    if text.lower() == probe_seed.lower():
        # The suggestion is just the seed echoed back -- no added information.
        return None
    return {
        "title_hint": text,
        "seed": probe_seed,
        "seed_origin": seed_item.origin,
        "category_id": seed_item.category_id,
        "anchor": seed_item.anchor,
        "base_seed": seed_item.base_seed,
        "matched_terms": _matched(text, vocabulary),
    }


def _matched(text: str, terms: Iterable[str]) -> list[str]:
    lowered = text.lower()
    return [t for t in terms if t and t in lowered]


def _tag_duplicates(candidates: list[dict[str, Any]]) -> None:
    """Mark trailing exact/near-duplicate candidates in place (does not drop them)."""
    accepted: list[str] = []
    for candidate in candidates:
        key = candidate["title_hint"].lower()
        is_duplicate = key in accepted or any(
            SequenceMatcher(None, key, other).ratio() >= NEAR_DUPLICATE_SIMILARITY for other in accepted
        )
        candidate["duplicate"] = is_duplicate
        if not is_duplicate:
            accepted.append(key)


# ---------------------------------------------------------------------------
# [B] Batch expand
# ---------------------------------------------------------------------------

def batch_expand(
    seeds: list[topic_seed_pool.SeedItem],
    vocabulary: Iterable[str],
    *,
    sleep_ms: int = 200,
    fetch: Callable[..., Any] | None = None,
    include_youtube: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run trend_probe.probe() per seed. Returns (candidates, seed_reports).

    An anchored seed that comes back `off_topic` is retried bare once. Compound
    queries like `치매 고혈압` can return too few suggestions to clear
    trend_probe's MIN_KEPT sample-size gate, and an empty queue is worse than a
    wide one — the bare seed's off-domain suggestions get caught downstream by
    the `no_domain_anchor` rejection instead.
    """
    vocabulary = tuple(vocabulary)
    candidates: list[dict[str, Any]] = []
    seed_reports: list[dict[str, Any]] = []

    for index, seed_item in enumerate(seeds):
        probe_seed = seed_item.seed
        result = trend_probe.probe(probe_seed, vocabulary, include_youtube=include_youtube, fetch=fetch)

        fell_back = False
        if not result.ok and seed_item.anchored and seed_item.base_seed:
            fell_back = True
            probe_seed = seed_item.base_seed
            result = trend_probe.probe(probe_seed, vocabulary, include_youtube=include_youtube, fetch=fetch)

        seed_reports.append({
            "seed": seed_item.seed, "origin": seed_item.origin, "ok": result.ok,
            "language": result.language, "status": result.status,
            "keyword_count": len(result.keywords),
            "anchor": seed_item.anchor, "fell_back": fell_back,
        })
        if result.ok:
            source = f"suggest_{result.language}"
            for keyword in result.keywords:
                candidate = _normalize_candidate(keyword, seed_item, vocabulary, probe_seed=probe_seed)
                if candidate is None:
                    continue
                candidate["language"] = result.language
                candidate["domain_match"] = result.domain_match
                candidate["source"] = source
                candidates.append(candidate)
        if sleep_ms and index < len(seeds) - 1:
            time.sleep(sleep_ms / 1000)

    _tag_duplicates(candidates)
    return candidates, seed_reports


# ---------------------------------------------------------------------------
# [D] Score + rank
# ---------------------------------------------------------------------------

def _recent_titles(conn, lookback_days: int) -> list[str]:
    if conn is None:
        return []
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
        rows = conn.execute("SELECT title FROM videos WHERE published_at >= ?", (cutoff,))
        return [str(row[0]) for row in rows if row[0]]
    except Exception:
        return []


def score_and_rank(
    candidates: list[dict[str, Any]],
    *,
    vocabulary: Iterable[str],
    recent_titles: list[str],
    rules: dict[str, Any],
    domain: topic_domain.Domain | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Returns (eligible, rejected), both sorted by score descending.

    `eligible` holds every candidate that cleared the threshold and is not a
    duplicate/banned/off-domain -- callers apply eligible_top_k at write time.

    `no_domain_anchor` is checked before the threshold on purpose: a candidate
    outside the channel's base domain is not a low-scoring topic, it is the
    wrong topic, and no amount of search-intent points should rescue it.
    """
    domain = domain or topic_domain.load_domain()
    threshold = rules.get("threshold", topic_score.DEFAULT_RULES["threshold"])
    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        result = topic_score.score_candidate(
            candidate["title_hint"], vocabulary=vocabulary, recent_titles=recent_titles,
            rules=rules, domain=domain,
        )
        record = dict(candidate)
        record["score"] = result.total
        record["score_breakdown"] = result.breakdown
        record["matched_anchors"] = list(result.matched_anchors)
        record["matched_terms"] = list(dict.fromkeys(list(candidate.get("matched_terms", ())) + list(result.matched_terms)))
        if candidate.get("duplicate"):
            record["reject_reason"] = "duplicate"
        elif result.banned:
            record["reject_reason"] = "ban_keyword"
        elif result.missing_anchor:
            record["reject_reason"] = "no_domain_anchor"
        elif result.total < threshold:
            record["reject_reason"] = "below_threshold"
        scored.append(record)

    scored.sort(key=lambda r: r["score"], reverse=True)
    eligible = [r for r in scored if "reject_reason" not in r]
    rejected = [r for r in scored if "reject_reason" in r]
    return eligible, rejected


# ---------------------------------------------------------------------------
# [E] Write queues
# ---------------------------------------------------------------------------

def _build_topic_payload(
    record: dict[str, Any], topic_id: str, created_at: str, status: str,
    domain_id: str = "",
) -> dict[str, Any]:
    return {
        "topic_id": topic_id,
        "title_hint": record["title_hint"],
        "seed": record["seed"],
        "category_id": record.get("category_id"),
        "source": record.get("source", ""),
        "score": record["score"],
        "score_breakdown": record["score_breakdown"],
        "language": record.get("language", ""),
        "domain_match": record.get("domain_match", 0.0),
        "domain_id": domain_id,
        "matched_anchors": record.get("matched_anchors", []),
        "matched_terms": record.get("matched_terms", []),
        "raw_signals": {
            "seed_origin": record.get("seed_origin", ""),
            "anchor": record.get("anchor"),
            "base_seed": record.get("base_seed"),
        },
        # A hint for the story_type picker, not a decision: story_type stays
        # null until objective_planner consumes this candidate and spends a
        # slot of the configured mix on it (design spec §2.4).
        "suggested_story_types": story_types.suggest_from_text(record["title_hint"]),
        "story_type": None,
        "created_at": created_at,
        "status": status,
        "consumed": False,
    }


def write_queues(
    eligible: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    run_meta: dict[str, Any],
    *,
    base_dir: str | Path | None = None,
    eligible_top_k: int = 15,
    domain_id: str = "",
) -> dict[str, Any]:
    base = Path(base_dir) if base_dir else DATA_TOPICS_DIR
    raw_dir, eligible_dir, rejected_dir = base / "raw", base / "eligible", base / "rejected"
    for directory in (raw_dir, eligible_dir, rejected_dir):
        directory.mkdir(parents=True, exist_ok=True)

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    stamp = now.strftime("%Y-%m-%d_%H%M")
    created_at = now.isoformat(timespec="seconds")

    top = eligible[:eligible_top_k]
    written_eligible: list[Path] = []
    for rank, record in enumerate(top, start=1):
        topic_id = f"{date_str}_{rank:02d}_{_slugify(record['title_hint'])}"
        payload = _build_topic_payload(record, topic_id, created_at, status="eligible", domain_id=domain_id)
        path = eligible_dir / f"{topic_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written_eligible.append(path)

    written_rejected: list[Path] = []
    for rank, record in enumerate(rejected, start=1):
        topic_id = f"{date_str}_{rank:02d}_{_slugify(record['title_hint'])}"
        payload = _build_topic_payload(record, topic_id, created_at, status="rejected", domain_id=domain_id)
        payload["reject_reason"] = record.get("reject_reason", "below_threshold")
        path = rejected_dir / f"{topic_id}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written_rejected.append(path)

    run_payload = {
        **run_meta,
        "created_at": created_at,
        "candidate_count": len(eligible) + len(rejected),
        "eligible_written_count": len(written_eligible),
        "eligible_over_top_k_count": max(0, len(eligible) - len(written_eligible)),
        "rejected_count": len(written_rejected),
    }
    raw_path = raw_dir / f"{stamp}_run.json"
    raw_path.write_text(json.dumps(run_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {"raw": raw_path, "eligible": written_eligible, "rejected": written_rejected}


# ---------------------------------------------------------------------------
# End-to-end refresh
# ---------------------------------------------------------------------------

_UNSET = object()


def run_refresh(
    *,
    categories_path: str | Path | None = None,
    pipeline_config: dict[str, Any] | None = None,
    score_rules: dict[str, Any] | None = None,
    conn: Any = _UNSET,
    fetch: Callable[..., Any] | None = None,
    base_dir: str | Path | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    domain: topic_domain.Domain | None = None,
) -> dict[str, Any]:
    """`conn` defaults to a real youtube_feedback.db connection. Pass conn=None
    explicitly (as tests do) to run with no database at all."""
    pipeline_config = pipeline_config or load_pipeline_config()
    score_rules = score_rules or topic_score.load_rules()
    domain = domain or topic_domain.load_domain()

    owns_conn = conn is _UNSET
    if owns_conn:
        conn = _default_conn()

    try:
        vocabulary = trend_probe.build_channel_vocabulary(conn, categories_path)

        seeds = topic_seed_pool.build_seed_pool(
            conn, categories_path, pipeline_config.get("extra_seeds", []),
            max_seeds_total=pipeline_config["max_seeds_total"],
            max_seeds_per_category=pipeline_config["max_seeds_per_category"],
            min_seed_len=pipeline_config["min_seed_len"],
            max_seed_len=pipeline_config["max_seed_len"],
            domain=domain,
        )

        candidates, seed_reports = batch_expand(
            seeds, vocabulary, sleep_ms=pipeline_config.get("suggest_sleep_ms", 200), fetch=fetch,
        )

        lookback_days = score_rules.get("novelty", {}).get("lookback_days", 60)
        recent_titles = _recent_titles(conn, lookback_days)

        eligible, rejected = score_and_rank(
            candidates, vocabulary=vocabulary, recent_titles=recent_titles,
            rules=score_rules, domain=domain,
        )
    finally:
        if owns_conn and conn is not None:
            conn.close()

    top_k = pipeline_config.get("eligible_top_k", score_rules.get("eligible_top_k", 15))
    if limit is not None:
        top_k = min(top_k, limit)

    run_meta = {
        "seed_count": len(seeds),
        "seed_origins": {origin: sum(1 for s in seeds if s.origin == origin) for origin in ("research_categories", "keywords_db", "extra")},
        "candidate_count": len(candidates),
        "domain_id": domain.domain_id,
        # If anchor_fallback_count approaches anchored_seed_count, anchoring is
        # starving the probe and seed_anchors needs a more common phrasing.
        "anchored_seed_count": sum(1 for s in seeds if s.anchored),
        "anchor_fallback_count": sum(1 for r in seed_reports if r.get("fell_back")),
        "seed_reports": seed_reports,
    }

    if dry_run:
        return {"eligible": eligible[:top_k], "rejected": rejected, "run_meta": run_meta, "written": None}

    written = write_queues(
        eligible, rejected, run_meta, base_dir=base_dir, eligible_top_k=top_k,
        domain_id=domain.domain_id,
    )
    return {"eligible": eligible[:top_k], "rejected": rejected, "run_meta": run_meta, "written": written}


# ---------------------------------------------------------------------------
# Consumption (used by 0_topic_plan.py / run_pipeline.py --from-eligible)
# ---------------------------------------------------------------------------

def list_eligible(base_dir: str | Path | None = None, include_consumed: bool = False) -> list[dict[str, Any]]:
    base = Path(base_dir) if base_dir else DATA_TOPICS_DIR
    eligible_dir = base / "eligible"
    if not eligible_dir.exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(eligible_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if include_consumed or not payload.get("consumed"):
            payload["_path"] = str(path)
            items.append(payload)
    items.sort(key=lambda item: item.get("score", 0), reverse=True)
    return items


def top_candidates(n: int = 3, base_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """The n highest-scoring candidates a human has not picked yet.

    Narrower than `list_eligible()` by one rule: an already-selected candidate
    drops out of the recommendation list, so pressing "refresh" after a pick
    surfaces the next one down instead of re-offering the same topic.
    """
    items = [c for c in list_eligible(base_dir=base_dir, include_consumed=False)
             if c.get("status") != STATUS_SELECTED]
    return items[:max(0, n)]


def select_topic(topic_id: str, base_dir: str | Path | None = None) -> dict[str, Any] | None:
    """Record a human's pick. Returns the selected payload, or None if the
    topic_id is unknown.

    `consumed` is deliberately left alone: "selected" means a person chose this
    topic, "consumed" means the pipeline actually spent it. Keeping them apart
    leaves `pick_top_eligible()`'s existing meaning intact and gives the
    eventual pipeline hookup something unambiguous to read.

    Unknown ids return None rather than raising -- a stale Slack button from a
    queue that has since been refreshed must not crash the bot.
    """
    base = Path(base_dir) if base_dir else DATA_TOPICS_DIR
    path = base / "eligible" / f"{topic_id}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    payload["status"] = STATUS_SELECTED
    payload["selected_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    selected_path = base / SELECTED_FILENAME
    selected_path.parent.mkdir(parents=True, exist_ok=True)
    selected_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def current_selection(base_dir: str | Path | None = None) -> dict[str, Any] | None:
    """The pick recorded by the most recent select_topic(), if any."""
    base = Path(base_dir) if base_dir else DATA_TOPICS_DIR
    try:
        return json.loads((base / SELECTED_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def render_candidate_lines(candidates: list[dict[str, Any]]) -> list[str]:
    """One numbered Korean line per candidate, shared by the CLI and the Slack
    card so both explain a recommendation the same way."""
    lines: list[str] = []
    for rank, candidate in enumerate(candidates, start=1):
        breakdown = candidate.get("score_breakdown") or {}
        strongest = sorted(breakdown.items(), key=lambda kv: kv[1], reverse=True)[:2]
        reasons = ", ".join(SCORE_LABELS.get(key, key) for key, value in strongest if value > 0)
        parts = [f"점수 {candidate.get('score', 0)}", f"시드 {candidate.get('seed', '-')}"]
        if reasons:
            parts.append(reasons)
        lines.append(f"{rank}. {candidate.get('title_hint', '')}  ({' · '.join(parts)})")
    return lines


def pick_top_eligible(base_dir: str | Path | None = None, mark_consumed: bool = True) -> dict[str, Any] | None:
    """Highest-scoring unconsumed eligible candidate, or None if the queue is empty.

    When `mark_consumed` (the default for real consumption paths), the source
    JSON is rewritten with consumed=true before the candidate is returned, so
    a second call in the same run picks the next one down.
    """
    candidates = list_eligible(base_dir=base_dir, include_consumed=False)
    if not candidates:
        return None
    top = dict(candidates[0])
    path = Path(top.pop("_path"))
    if mark_consumed:
        top["consumed"] = True
        path.write_text(json.dumps(top, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return top


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def cmd_refresh(args: argparse.Namespace) -> int:
    result = run_refresh(base_dir=args.base_dir)
    meta = result["run_meta"]
    print(f"시드 {meta['seed_count']}개 → 후보 {meta['candidate_count']}개 → eligible {len(result['eligible'])}개")
    if result["written"]:
        print(f"raw: {result['written']['raw']}")
        print(f"eligible: {len(result['written']['eligible'])}건, rejected: {len(result['written']['rejected'])}건")
    return 0


def cmd_dry_run(args: argparse.Namespace) -> int:
    result = run_refresh(base_dir=args.base_dir, dry_run=True, limit=args.limit)
    if not result["eligible"]:
        print("eligible 후보가 없습니다.")
        return 0
    for rank, record in enumerate(result["eligible"][:args.limit], start=1):
        print(f"{rank}. {record['title_hint']} (score={record['score']}, seed={record['seed']})")
    return 0


def cmd_score_text(args: argparse.Namespace) -> int:
    """Score one arbitrary string with no network and no queue writes.

    This is how the threshold gets calibrated after a weight change, and how an
    operator answers "why did that get rejected" later on.
    """
    domain = topic_domain.load_domain()
    rules = topic_score.load_rules()
    conn = _default_conn()
    try:
        vocabulary = trend_probe.build_channel_vocabulary(conn)
        recent_titles = _recent_titles(conn, rules.get("novelty", {}).get("lookback_days", 60))
    finally:
        if conn is not None:
            conn.close()

    result = topic_score.score_candidate(
        args.score_text, vocabulary=vocabulary, recent_titles=recent_titles,
        rules=rules, domain=domain,
    )
    threshold = rules.get("threshold", topic_score.DEFAULT_RULES["threshold"])
    if result.missing_anchor:
        verdict = "탈락 (no_domain_anchor)"
    elif result.banned:
        verdict = "탈락 (ban_keyword)"
    elif result.total < threshold:
        verdict = f"탈락 (below_threshold, 임계값 {threshold})"
    else:
        verdict = f"통과 (임계값 {threshold})"

    print(f"베이스 분야: {topic_domain.describe(domain)}")
    print(f"후보: {args.score_text}")
    print(f"점수 {result.total} → {verdict}")
    for key, value in result.breakdown.items():
        print(f"  {SCORE_LABELS.get(key, key):<8} {value:>6}  (가중치 {rules.get('weights', {}).get(key, '-')})")
    print(f"앵커 매칭: {', '.join(result.matched_anchors) or '없음'}")
    print(f"어휘 매칭: {', '.join(result.matched_terms) or '없음'}")
    return 0


def cmd_list_eligible(args: argparse.Namespace) -> int:
    items = list_eligible(base_dir=args.base_dir)
    if not items:
        print("eligible 후보가 없습니다. --refresh를 먼저 실행하세요.")
        return 0
    for rank, item in enumerate(items, start=1):
        print(f"{rank}. {item['title_hint']} (score={item['score']}, seed={item['seed']}, topic_id={item['topic_id']})")
    return 0


def cmd_top(args: argparse.Namespace) -> int:
    """Recommend, don't decide -- printing the shortlist is the whole job here.
    Selecting is a separate, explicit command."""
    candidates = top_candidates(args.top, base_dir=args.base_dir)
    selected = current_selection(base_dir=args.base_dir)
    if selected:
        print(f"현재 선택: {selected.get('title_hint')} (topic_id={selected.get('topic_id')})\n")
    if not candidates:
        print("추천할 후보가 없습니다. --refresh를 먼저 실행하세요.")
        return 0
    for line in render_candidate_lines(candidates):
        print(line)
    print(f"\n선택: --select-rank 1 (또는 --select {candidates[0]['topic_id']})")
    return 0


def _report_selection(selected: dict[str, Any]) -> int:
    print(f"선택: {selected.get('title_hint')}")
    print(f"topic_id: {selected.get('topic_id')}")
    print(f"점수: {selected.get('score')} · 시드: {selected.get('seed')}")
    print("선택 기록 완료. 파이프라인 연결은 아직 수동입니다.")
    return 0


def cmd_select(args: argparse.Namespace) -> int:
    topic_id = args.select
    if args.select_rank is not None:
        candidates = top_candidates(max(args.select_rank, DEFAULT_TOP_N), base_dir=args.base_dir)
        if not 1 <= args.select_rank <= len(candidates):
            print(f"{args.select_rank}번 후보가 없습니다. --top으로 목록을 먼저 확인하세요.", file=sys.stderr)
            return 1
        topic_id = candidates[args.select_rank - 1]["topic_id"]

    selected = select_topic(topic_id, base_dir=args.base_dir)
    if selected is None:
        print(f"해당 후보를 찾지 못했습니다: {topic_id}", file=sys.stderr)
        return 1
    return _report_selection(selected)


def cmd_selected(args: argparse.Namespace) -> int:
    selected = current_selection(base_dir=args.base_dir)
    if selected is None:
        print("선택된 주제가 없습니다. --top으로 후보를 확인한 뒤 --select-rank로 선택하세요.")
        return 0
    print(f"선택 시각: {selected.get('selected_at', '-')}")
    return _report_selection(selected)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="시드 없이 채널 최적화 주제 후보 생성 (Phase 1)")
    parser.add_argument("--base-dir", default=None, help="data/topics 상위 디렉터리 (테스트/오버라이드용)")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--refresh", action="store_true", help="시드 풀 → probe → 점수 → eligible/rejected 큐 갱신")
    mode.add_argument("--dry-run", action="store_true", help="기록 없이 상위 후보만 출력")
    mode.add_argument("--list-eligible", action="store_true", help="현재 eligible 큐 목록 출력")
    mode.add_argument("--top", type=int, nargs="?", const=DEFAULT_TOP_N, default=None,
                      metavar="N", help=f"상위 N개 추천 출력 (기본 {DEFAULT_TOP_N})")
    mode.add_argument("--select", metavar="TOPIC_ID", help="topic_id로 주제 선택 기록")
    mode.add_argument("--select-rank", type=int, metavar="N", help="--top 목록의 N번을 선택 기록")
    mode.add_argument("--selected", action="store_true", help="현재 선택된 주제 조회")
    mode.add_argument("--score-text", metavar="TEXT",
                      help="임의 문장의 점수·앵커 매칭 출력 (네트워크 호출 없음, 큐 미변경)")
    parser.add_argument("--limit", type=int, default=20, help="--dry-run 출력 개수")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.refresh:
        return cmd_refresh(args)
    if args.dry_run:
        return cmd_dry_run(args)
    if args.score_text:
        return cmd_score_text(args)
    if args.top is not None:
        return cmd_top(args)
    if args.select or args.select_rank is not None:
        return cmd_select(args)
    if args.selected:
        return cmd_selected(args)
    return cmd_list_eligible(args)


if __name__ == "__main__":
    raise SystemExit(main())
