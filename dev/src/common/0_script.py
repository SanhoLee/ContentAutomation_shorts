import argparse
import importlib.util
import json
import os
import re
import sys
import time
from collections import defaultdict
from pathlib import Path
from urllib.parse import quote

import content_package
import evidence_probe
import hook_types
import story_types
from claude_cost import assert_budget, record_usage, web_search_total
# Both live in scene_visuals because that stage runs without Stage 2's env.
from scene_visuals import clip_at_word_boundary, positional_role
from script_runtime import (
    load_creative_dna,
    load_runtime_settings,
    load_story_common_template,
    load_story_template,
)

SETTINGS = load_runtime_settings()
WORK_DIR = SETTINGS.work_dir
os.makedirs(WORK_DIR, exist_ok=True)

ATEMPO = SETTINGS.atempo
TARGET_DURATION_SEC = SETTINGS.target_duration_sec
SPEECH_PACE = SETTINGS.speech_pace
SCRIPT_DENSITY = SETTINGS.script_density
TREND_CANDIDATE_COUNT = SETTINGS.trend_candidate_count
REQUEST_TIMEOUT = SETTINGS.request_timeout
CLAUDE_TIMEOUT = SETTINGS.claude_timeout
CLAUDE_HTTP_RETRIES = SETTINGS.claude_http_retries
PUBMED_QUERY_TIMEOUT = SETTINGS.pubmed_query_timeout
PUBMED_RETMAX = SETTINGS.pubmed_retmax
PUBMED_ABSTRACT_CHAR_LIMIT = SETTINGS.pubmed_abstract_char_limit
NCBI_API_KEY = SETTINGS.ncbi_api_key
CLAUDE_SCRIPT_MODEL = SETTINGS.claude_script_model
CLAUDE_QUERY_MODEL = SETTINGS.claude_query_model
CLAUDE_RESEARCH_MODEL = SETTINGS.claude_research_model
CLAUDE_STRATEGY_MODEL = SETTINGS.claude_strategy_model
CLAUDE_STRATEGY_FALLBACK_MODELS = SETTINGS.claude_strategy_fallback_models
CLAUDE_STRATEGY_MAX_TOKENS = SETTINGS.claude_strategy_max_tokens
MAX_TOKENS = SETTINGS.max_tokens
ENABLE_WEB_RESEARCH = SETTINGS.enable_web_research
WEB_RESEARCH_TIMEOUT = SETTINGS.web_research_timeout
WEB_RESEARCH_MAX_USES = SETTINGS.web_research_max_uses
WEB_RESEARCH_MAX_TOKENS = SETTINGS.web_research_max_tokens
WEB_RESEARCH_MAX_TOOL_TURNS = SETTINGS.web_research_max_tool_turns
ENABLE_CASE_RESEARCH = SETTINGS.enable_case_research
CASE_RESEARCH_TIMEOUT = SETTINGS.case_research_timeout
CASE_RESEARCH_MAX_USES = SETTINGS.case_research_max_uses
CASE_RESEARCH_MAX_TOKENS = SETTINGS.case_research_max_tokens
CASE_RESEARCH_MAX_TOOL_TURNS = SETTINGS.case_research_max_tool_turns
RESEARCH_MODE = SETTINGS.research_mode
STRATEGY_PATH = SETTINGS.strategy_path
YOUTUBE_FEEDBACK_STRICTNESS = SETTINGS.youtube_feedback_strictness
YOUTUBE_FEEDBACK_AUTO_SYNC = SETTINGS.youtube_feedback_auto_sync
total_chars = SETTINGS.total_chars
prompt_target_chars = SETTINGS.prompt_target_chars
min_scenes_estimate = SETTINGS.min_scenes_estimate
USE_CREATIVE_DNA = SETTINGS.use_creative_dna
USE_STORY_TYPES = SETTINGS.use_story_types
MAX_SCRIPT_LENGTH_RATIO = 1.40
PROMPT_MIN_LENGTH_RATIO = 0.90
PROMPT_MAX_LENGTH_RATIO = 1.15

TREND_CANDIDATES_PATH = os.path.join(WORK_DIR, "trend_candidates.json")
PUBMED_STATUS_PATH = os.path.join(WORK_DIR, "pubmed_status.json")
CLAUDE_USAGE_PATH = os.path.join(WORK_DIR, "claude_usage.jsonl")
def is_claude_transient_status(status_code):
    """429 (rate limit) plus any 5xx.

    Covers Anthropic's own errors (500/502/503/529 "overloaded") and the
    Cloudflare edge in front of api.anthropic.com (520 "unknown error",
    521-524) -- all mean "try again," not "the request was bad." An
    enumerated tuple missed 520 and crashed a job outright instead of
    retrying (see KNOWN_ISSUES.md).
    """
    return status_code == 429 or status_code >= 500


def creative_dna_block():
    """Channel tone/style guide, prepended identically to Stage 1 and Stage 2
    prompts (see docs/design Phase 2). USE_CREATIVE_DNA=off restores the
    pre-Phase-2 prompt exactly, for A/B comparison or rollback."""
    if not USE_CREATIVE_DNA:
        return ""
    dna = load_creative_dna()
    if not dna:
        return ""
    return f"[채널 크리에이티브 DNA — 항상 이 톤과 문체를 따르세요]\n{dna}\n\n"


def strategy_story_type(strategy):
    """story_type already decided for this job, or None.

    Checked in the order it becomes authoritative: an explicit field on the
    strategy, then the goal planner's content_design, then whatever the
    format_type maps to. Returns None only when the job genuinely has no
    genre yet, which is Stage 1's cue to choose one.
    """
    strategy = strategy or {}
    design = strategy.get("content_design")
    design = design if isinstance(design, dict) else {}
    return (
        story_types.normalize(strategy.get("story_type"))
        or story_types.normalize(design.get("story_type"))
        or story_types.story_type_for_format(strategy.get("format_type") or design.get("format_type"))
    )


def apply_story_type(strategy, fallback=None):
    """Settle story_type/format_type on a strategy dict, in place.

    story_type is the source of truth: a format_type that maps to a different
    genre is rewritten to the mapped value and the rewrite is printed, per
    design spec §3. content_design keeps a mirrored copy so Stage 2 and
    downstream readers see the same pair wherever they look.
    """
    if not USE_STORY_TYPES:
        return strategy
    design = strategy.get("content_design")
    design = design if isinstance(design, dict) else None
    resolved = strategy_story_type(strategy) or story_types.normalize(fallback)
    raw_format = strategy.get("format_type") or (design or {}).get("format_type")
    story_type, format_type, warning = story_types.reconcile(resolved, raw_format)
    if warning:
        print(f"⚠️  {warning}")
    strategy["story_type"] = story_type
    strategy["format_type"] = format_type
    if design is not None:
        design["story_type"] = story_type
        design["format_type"] = format_type
    return strategy


def story_type_directive(story_type, decided):
    """The one instruction block that carries the genre into a Claude prompt.

    `decided` distinguishes the two cases the spec calls out (§5.3): a genre
    handed down by the planner must not be changed, while a job that arrives
    without one lets Stage 1 choose and say why.
    """
    if not USE_STORY_TYPES:
        return ""
    if decided and story_type:
        return (
            f"\n\n[이번 영상의 story_type — 이미 확정됨, 변경 금지]\n"
            f"{story_types.describe(story_type)}\n"
            f"이 타입의 훅과 전개 의도를 전략에 반영하세요. story_type 필드에는 "
            f"'{story_type}'을 그대로 출력하세요."
        )
    options = "\n".join(f"- {story_types.describe(item)}" for item in story_types.STORY_TYPES)
    return (
        "\n\n[이번 영상의 story_type — 아직 정해지지 않음]\n"
        f"{options}\n"
        "주제에 가장 맞는 하나를 골라 story_type 필드에 id를 그대로 출력하고, "
        "고른 이유를 story_type_reason에 한 줄로 쓰세요."
    )


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Brain50 Shorts script generator (2-stage)")
    parser.add_argument("topic", nargs="*", help="아이디어 또는 주제 문장")
    parser.add_argument("--topic-json", help="구조화된 topic JSON 파일 경로 (main_keyword 등 포함)")
    parser.add_argument("--trend",       help="키워드 후보를 뽑을 씨드 단어")
    parser.add_argument("--trend-choice", type=int, help="trend_candidates.json에서 선택할 번호")
    parser.add_argument("--allow-no-pubmed", action="store_true")
    parser.add_argument("--no-web-research", action="store_true")
    parser.add_argument("--skip-strategy",   action="store_true",
                        help="strategy.json이 이미 있으면 Stage 1 건너뜀")
    return parser.parse_args()


# ─────────────────────────────────────────────
# HTTP 유틸
# ─────────────────────────────────────────────

def request_json(url, params=None, headers=None):
    try:
        import requests
        res = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        res.raise_for_status()
        text = res.text.strip()
    except ModuleNotFoundError:
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen
        if params:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urlencode(params)}"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
        with urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            text = r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace").strip()
    if text.startswith(")]}'"):
        text = text.split("\n", 1)[1]
    return json.loads(text)


def request_text(url, params=None, headers=None):
    try:
        import requests
        res = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
        res.raise_for_status()
        return res.text
    except ModuleNotFoundError:
        from urllib.parse import urlencode
        from urllib.request import Request, urlopen
        if params:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urlencode(params)}"
        req = Request(url, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
        with urlopen(req, timeout=REQUEST_TIMEOUT) as r:
            return r.read().decode(r.headers.get_content_charset() or "utf-8", errors="replace")



def describe_http_error(response):
    """Return an actionable, compact HTTP error message for Claude/API logs."""
    try:
        body = response.json()
    except Exception:
        body = response.text
    if not isinstance(body, str):
        body = json.dumps(body, ensure_ascii=False)
    body = re.sub(r"\s+", " ", body).strip()
    if len(body) > 700:
        body = body[:700] + "..."
    return f"HTTP {response.status_code}: {body}"


def strategy_model_candidates():
    candidates = [CLAUDE_STRATEGY_MODEL, *CLAUDE_STRATEGY_FALLBACK_MODELS, CLAUDE_SCRIPT_MODEL]
    seen = set()
    unique = []
    for model in candidates:
        if model and model not in seen:
            seen.add(model)
            unique.append(model)
    return unique

# ─────────────────────────────────────────────
# 트렌드 후보
# ─────────────────────────────────────────────


def record_claude_usage(stage, model, response, attempt=1, success=True):
    return record_usage(
        stage,
        model,
        response,
        jsonl_path=CLAUDE_USAGE_PATH,
        job_id=os.environ.get("JOB_ID"),
        plan_id=_active_plan_id(),
        attempt=attempt,
        success=success,
    )


def _active_plan_id():
    plan_path = Path(WORK_DIR) / "topic_plan.json"
    if not plan_path.is_file():
        return None
    try:
        return int((json.loads(plan_path.read_text(encoding="utf-8")).get("objective") or {}).get("plan_id"))
    except (TypeError, ValueError, json.JSONDecodeError, OSError):
        return None


def enforce_claude_budget(anticipated_cost_usd=0.0):
    assert_budget(
        job_id=os.environ.get("JOB_ID"),
        job_budget_usd=SETTINGS.claude_job_budget_usd,
        daily_budget_usd=SETTINGS.claude_daily_budget_usd,
        anticipated_cost_usd=anticipated_cost_usd,
    )


def call_claude_api(payload, timeout, label, stage):
    import requests

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(f"{label} 호출에 필요한 ANTHROPIC_API_KEY가 없습니다.")

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    attempts = CLAUDE_HTTP_RETRIES + 1
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            enforce_claude_budget()
            print(f"{label} 호출 시도 {attempt}/{attempts} (timeout={timeout}s)")
            res = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            if res.status_code >= 400:
                try:
                    failed_response = res.json()
                except Exception:
                    failed_response = {}
                failed_response["_request_id"] = res.headers.get("request-id")
                record_claude_usage(
                    stage, payload.get("model", ""), failed_response,
                    attempt=attempt, success=False,
                )
            if is_claude_transient_status(res.status_code):
                last_error = requests.HTTPError(f"Claude transient status {res.status_code}: {res.text[:500]}")
                if attempt <= CLAUDE_HTTP_RETRIES:
                    print(f"{label} 일시 오류 {res.status_code}. 재시도합니다.")
                    time.sleep(min(10 * attempt, 30))
                    continue
            res.raise_for_status()
            data = res.json()
            data["_request_id"] = (
                res.headers.get("request-id")
                or res.headers.get("anthropic-request-id")
                or res.headers.get("x-request-id")
            )
            record_claude_usage(stage, payload.get("model", ""), data)
            return data
        except requests.ReadTimeout as exc:
            record_claude_usage(stage, payload.get("model", ""), {}, attempt=attempt, success=False)
            raise RuntimeError(
                f"{label} 응답 대기 시간이 초과되었습니다. 이미 서버에서 처리 중일 수 있어 "
                "자동 재시도하지 않습니다. 같은 JOB을 바로 재시작하면 중복 과금될 수 있습니다."
            ) from exc
        except (requests.ConnectTimeout, requests.ConnectionError, requests.Timeout) as exc:
            record_claude_usage(stage, payload.get("model", ""), {}, attempt=attempt, success=False)
            raise RuntimeError(
                f"{label} 네트워크 타임아웃/연결 오류가 발생했습니다. 중복 과금 방지를 위해 "
                "자동 재시도하지 않습니다."
            ) from exc

    raise RuntimeError(f"{label} 호출이 실패했습니다. 마지막 오류: {last_error}")


def limit_pubmed_abstracts(text):
    if PUBMED_ABSTRACT_CHAR_LIMIT <= 0 or len(text) <= PUBMED_ABSTRACT_CHAR_LIMIT:
        return text
    clipped = text[:PUBMED_ABSTRACT_CHAR_LIMIT].rsplit("\n\n", 1)[0].strip()
    if not clipped:
        clipped = text[:PUBMED_ABSTRACT_CHAR_LIMIT].strip()
    return clipped + "\n\n[입력 토큰 안정화를 위해 PubMed 초록 일부를 생략했습니다.]"
def fetch_google_suggestions(seed):
    data = request_json("https://suggestqueries.google.com/complete/search",
                        params={"client": "firefox", "hl": "ko", "gl": "KR", "ie": "utf-8", "oe": "utf-8", "q": seed})
    return data[1] if len(data) > 1 else []

def fetch_youtube_suggestions(seed):
    data = request_json("https://suggestqueries.google.com/complete/search",
                        params={"client": "firefox", "ds": "yt", "hl": "ko", "gl": "KR", "ie": "utf-8", "oe": "utf-8", "q": seed})
    return data[1] if len(data) > 1 else []

def fetch_google_trends_topics(seed):
    data = request_json(f"https://trends.google.com/trends/api/autocomplete/{quote(seed)}",
                        params={"hl": "ko", "tz": "-540"})
    return [t.get("title") for t in data.get("default", {}).get("topics", []) if t.get("title")]

def normalize_keyword(text):
    return re.sub(r"\s+", " ", str(text)).strip(" \t\n\r-_/|,.")

def collect_trend_candidates(seed):
    sources = {
        "google_suggest":  fetch_google_suggestions,
        "youtube_suggest": fetch_youtube_suggestions,
        "google_trends":   fetch_google_trends_topics,
    }
    grouped = defaultdict(set)
    errors  = {}
    for name, fn in sources.items():
        try:
            for kw in fn(seed):
                n = normalize_keyword(kw)
                if n and len(n) <= 40:
                    grouped[n].add(name)
        except Exception as e:
            errors[name] = str(e)

    scored = []
    for kw, srcs in grouped.items():
        score = len(srcs) * 10
        if seed.replace(" ", "") in kw.replace(" ", ""): score += 3
        if 4 <= len(kw) <= 20: score += 2
        scored.append({"keyword": kw, "sources": sorted(srcs), "score": score})
    scored.sort(key=lambda x: (-x["score"], x["keyword"]))
    candidates = scored[:TREND_CANDIDATE_COUNT]

    payload = {"seed": seed, "candidates": candidates, "errors": errors}
    with open(TREND_CANDIDATES_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"트렌드 후보 저장: {TREND_CANDIDATES_PATH}")
    for i, item in enumerate(candidates, 1):
        print(f"{i}. {item['keyword']} ({', '.join(item['sources'])})")
    if not candidates:
        raise Exception("트렌드 후보를 찾지 못했습니다.")

def load_trend_choice(choice):
    if not os.path.exists(TREND_CANDIDATES_PATH):
        raise Exception("trend_candidates.json 없음. --trend 먼저 실행하세요.")
    with open(TREND_CANDIDATES_PATH, "r", encoding="utf-8") as f:
        payload = json.load(f)
    candidates = payload.get("candidates", [])
    idx = choice - 1
    if idx < 0 or idx >= len(candidates):
        raise Exception(f"번호 범위 초과: {choice}")
    selected = candidates[idx]
    return selected["keyword"], {"seed": payload.get("seed", ""), "selected": selected, "candidates": candidates}


# ─────────────────────────────────────────────
# PubMed
# ─────────────────────────────────────────────

class PubMedSearchError(Exception):
    pass

def assess_pubmed_query(topic, attempts=None):
    """Explain a miss using what the ladder actually tried.

    This used to only describe the problem — "핵심 키워드 2~4개로 줄여보세요" —
    while nothing in the code ever narrowed and retried. evidence_probe now does
    the narrowing, so this reports the outcome instead of prescribing work to a
    human who was never going to see it in an unattended run.
    """
    if attempts:
        tried = " → ".join(
            f"{a.get('rung')}({a.get('hits', a.get('error', '?'))})" for a in attempts
        )
        return f"검색어를 넓혀가며 시도했지만 초록을 찾지 못했습니다: {tried}"
    compact = re.sub(r"\s+", "", topic)
    if len(compact) <= 2:
        return "주제가 너무 짧습니다."
    if re.search(r"추천|가격|순위|고르는법|브랜드|후기|먹는법", topic):
        return "소비자형 키워드입니다. 효능/위험/기전 중심으로 바꿔보세요."
    return "PubMed에서 직접 맞는 초록을 찾지 못했습니다."

def write_pubmed_status(topic, pmids, status, message, abstracts_preview="", pubmed_query=None,
                        citations=None, ladder_rung=None, attempts=None):
    payload = {"topic": topic, "pubmed_query": pubmed_query or topic,
               "status": status, "pmids": pmids, "pmid_count": len(pmids),
               "message": message, "abstracts_preview": abstracts_preview[:1200],
               "citations": citations or [],
               # Which rung of the widening ladder produced this, so a reviewer
               # can tell an exact match from a category-level fallback.
               "ladder_rung": ladder_rung or "",
               "ladder_attempts": list(attempts or [])}
    with open(PUBMED_STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def contains_korean(text):
    return bool(re.search(r"[가-힣]", text or ""))

def clean_pubmed_query(query):
    query = re.sub(r"[`\"']", "", query or "")
    query = re.sub(r"\s+", " ", query).strip(" .;:-")
    if len(query) > 120:
        query = query[:120].rsplit(" ", 1)[0].strip()
    return query

def translate_pubmed_query(topic):
    if not contains_korean(topic):
        return clean_pubmed_query(topic) or topic

    payload = {
        "model": CLAUDE_QUERY_MODEL,
        "max_tokens": 120,
        "messages": [{
            "role": "user",
            "content": (
                "Convert the Korean health/medical content topic below into a concise English PubMed search query. "
                "Use 2 to 6 biomedical keywords, disease/risk/mechanism terms when relevant, and no Korean. "
                "If the topic compares named items, preserve the comparison intent and include the named targets as English search terms. "
                "Do not silently drop requested comparison targets. "
                "Do not add explanations, quotes, markdown, or Boolean operators unless essential.\n\n"
                f"Korean topic: {topic}"
            ),
        }],
    }
    try:
        response = call_claude_api(payload, PUBMED_QUERY_TIMEOUT, "PubMed 검색어 영어 변환", "pubmed_query")
        translated = clean_pubmed_query(response["content"][0]["text"])
        if translated and not contains_korean(translated):
            print(f"PubMed 검색어: {topic} -> {translated}")
            return translated
        print(f"PubMed 검색어 영어 변환 결과가 부적절합니다. 원문으로 검색합니다: {translated}")
    except RuntimeError as exc:
        print(f"PubMed 검색어 영어 변환 실패. 원문으로 검색합니다: {exc}")
    return topic

def _ncbi_params(params):
    """Merge in NCBI_API_KEY when configured (raises the anonymous 3 req/sec
    E-utilities limit to 10 req/sec); falls back to unauthenticated calls otherwise."""
    return {**params, "api_key": NCBI_API_KEY} if NCBI_API_KEY else params


def fetch_pubmed_citations(pmids):
    """Ground evidence_brief's source/year in real bibliographic data instead of
    letting Claude guess them from raw abstract text. Failure never blocks script
    generation — abstracts alone are still usable without this."""
    if not pmids:
        return [], ""
    try:
        data = request_json(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi",
            params=_ncbi_params({"db": "pubmed", "id": ",".join(pmids), "retmode": "json"}),
        )
    except Exception as exc:
        print(f"PubMed 서지 정보(esummary) 조회 실패(초록만으로 계속 진행): {type(exc).__name__}", file=sys.stderr)
        return [], ""
    result = data.get("result") or {}
    citations = []
    for pmid in result.get("uids") or pmids:
        doc = result.get(pmid) or {}
        year_match = re.search(r"\d{4}", str(doc.get("pubdate") or ""))
        citations.append({
            "pmid": pmid,
            "journal": " ".join(str(doc.get("source") or "").split()).strip(),
            "year": year_match.group(0) if year_match else "",
            "title": " ".join(str(doc.get("title") or "").split()).strip(),
        })
    lines = [
        f"- PMID {c['pmid']} · {c['journal'] or '출처 미상'} ({c['year'] or '연도 미상'}): {c['title']}"
        for c in citations if c["title"]
    ]
    block = (
        "[PubMed 서지 정보 — 실제 저널·연도. evidence_brief의 source/year는 이 값만 사용할 것]\n"
        + "\n".join(lines)
    ) if lines else ""
    return citations, block


def _category_fallback_query(topic):
    """The vetted English query for this topic's research category.

    This is the ladder's last rung and the reason a failed translation never
    has to fall back to Korean: research_categories.json already holds queries
    like "hearing loss AND dementia risk", verified when the category volumes
    were refreshed.
    """
    return evidence_probe.category_query_for(topic)


def fetch_pubmed_abstracts(topic):
    """Find abstracts, widening the query until something holds up.

    The old version issued one esearch and gave up on zero hits, which is how
    a job shipped with no evidence: `uncorrected refractive error dementia risk`
    returned nothing while `refractive error dementia` — the same query minus two
    modifiers — returns 30 papers.
    """
    pubmed_query = translate_pubmed_query(topic)
    found = evidence_probe.probe_pubmed(
        topic,
        pubmed_query,
        category_query=_category_fallback_query(topic),
        retmax=PUBMED_RETMAX,
        api_key=NCBI_API_KEY,
        fetch=lambda url, params: request_json(url, params=params),
    )
    attempts = [dict(a) for a in found.attempts]
    pmids = [p for p in (found.note or "").split(",") if p] if found.ok else []

    if found.ladder_rung not in ("full", "none"):
        print(f"PubMed 검색어 확장: {pubmed_query!r} -> {found.resolved_query!r} ({found.ladder_rung})")

    if not pmids:
        message = assess_pubmed_query(pubmed_query, attempts)
        write_pubmed_status(topic, pmids, "no_results", message,
                            pubmed_query=found.resolved_query or pubmed_query,
                            ladder_rung=found.ladder_rung, attempts=attempts)
        return "PubMed에서 직접 관련 초록을 찾지 못했습니다. 이 경우 논문 수치나 특정 연구 결과를 지어내지 말고, 신뢰 가능한 일반 의학 지식과 건강 커뮤니케이션 원칙을 바탕으로 조심스럽게 작성하세요. 근거가 불확실한 내용은 가능성이 있습니다, 도움될 수 있습니다처럼 표현하세요."
    pubmed_query = found.resolved_query

    citations, citation_block = fetch_pubmed_citations(pmids)

    text = request_text(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params=_ncbi_params({"db": "pubmed", "id": ",".join(pmids), "rettype": "abstract", "retmode": "text"}),
    )
    text = limit_pubmed_abstracts(text)
    if citation_block:
        text = citation_block + "\n\n" + text
    message = "PubMed 초록을 찾았습니다."
    if found.ladder_rung != "full":
        message += f" (검색어를 {found.ladder_rung} 단계로 넓혀 찾았습니다)"
    write_pubmed_status(topic, pmids, "ok", message, text, pubmed_query=pubmed_query,
                        citations=citations, ladder_rung=found.ladder_rung, attempts=attempts)
    return text


# ─────────────────────────────────────────────
# Claude 공통 호출 (tool_use 멀티턴 루프)
# ─────────────────────────────────────────────

def _call_claude_loop(
    messages, tools=None, max_tokens=1500, model=None, timeout=None,
    max_turns=10, stage="claude_loop",
):
    import requests
    headers = {"x-api-key": os.environ["ANTHROPIC_API_KEY"],
               "anthropic-version": "2023-06-01", "content-type": "application/json"}
    timeout = timeout or CLAUDE_TIMEOUT
    model   = model or CLAUDE_SCRIPT_MODEL
    current_messages = list(messages)

    for turn_index in range(max_turns):
        enforce_claude_budget()
        if tools and web_search_total(job_id=os.environ.get("JOB_ID")) >= SETTINGS.claude_max_web_searches_per_job:
            raise RuntimeError("작업별 Claude web_search 상한에 도달했습니다.")
        payload = {"model": model, "max_tokens": max_tokens, "messages": current_messages}
        if tools:
            payload["tools"] = tools
        res = requests.post("https://api.anthropic.com/v1/messages",
                            headers=headers, json=payload, timeout=timeout)
        if res.status_code >= 400:
            try:
                failed_response = res.json()
            except Exception:
                failed_response = {}
            failed_response["_request_id"] = res.headers.get("request-id")
            record_claude_usage(
                f"{stage}:turn_{turn_index + 1}", model, failed_response,
                attempt=turn_index + 1, success=False,
            )
        res.raise_for_status()
        data    = res.json()
        data["_request_id"] = (
            res.headers.get("request-id")
            or res.headers.get("anthropic-request-id")
            or res.headers.get("x-request-id")
        )
        record_claude_usage(f"{stage}:turn_{turn_index + 1}", model, data)
        content = data.get("content", [])
        current_messages.append({"role": "assistant", "content": content})

        if data.get("stop_reason") != "tool_use":
            return data

        tool_results = []
        for block in content:
            if block.get("type") != "tool_use": continue
            raw = block.get("content", "")
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": json.dumps(raw, ensure_ascii=False) if not isinstance(raw, str) else (raw or "No results."),
            })
        if tool_results:
            current_messages.append({"role": "user", "content": tool_results})
        else:
            break
    return data


# ─────────────────────────────────────────────
# web_search 보강
# ─────────────────────────────────────────────

def web_search_request_count(response):
    return ((response.get("usage") or {}).get("server_tool_use") or {}).get("web_search_requests", 0)


def web_search_error_codes(response):
    errors = []
    for block in response.get("content", []):
        if block.get("type") != "web_search_tool_result":
            continue
        content = block.get("content")
        if isinstance(content, dict) and content.get("type") == "web_search_tool_result_error":
            errors.append(content.get("error_code", "unknown"))
    return errors


CASE_SOURCE_DOMAINS = ("hidoc.co.kr", "mdtoday.co.kr", "k-health.com")
STAT_SOURCE_DOMAINS = (
    "hira.or.kr", "kdca.go.kr", "mohw.go.kr",
    "snuh.org", "amc.seoul.kr", "hqcenter.snu.ac.kr",
)


def fetch_web_research(topic, pubmed_query):
    print(
        "🔍 web_search 최신 영문 연구 자료 수집 중... "
        f"max_uses={WEB_RESEARCH_MAX_USES}, timeout={WEB_RESEARCH_TIMEOUT}s, "
        f"max_tokens={WEB_RESEARCH_MAX_TOKENS} (query: {pubmed_query})"
    )
    messages = [{"role": "user", "content":
        f"Search for recent (2022-2026) research about: {pubmed_query}\n\n"
        "Prioritize: Nature Neuroscience, Neuron, Journal of Neuroscience, PNAS, "
        "BrainFacts.org, Neuroscience News, Scientific American, The Transmitter, "
        "NIH/NINDS, Harvard Picower, MIT Brain & Cognitive Sciences, UCSF, Stanford, UCL\n\n"
        "Find 2-3 findings for a Korean health video targeting adults 50+. "
        "Focus on specific stats, sample sizes, percentages, actionable insights.\n"
        "Output: 4-6 concise bullet points in English with source name and year."}]
    try:
        data = _call_claude_loop(
            messages,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": WEB_RESEARCH_MAX_USES,
            }],
            max_tokens=WEB_RESEARCH_MAX_TOKENS,
            model=CLAUDE_RESEARCH_MODEL,
            timeout=WEB_RESEARCH_TIMEOUT,
            max_turns=WEB_RESEARCH_MAX_TOOL_TURNS,
            stage="web_research",
        )
        errors = web_search_error_codes(data)
        if errors:
            print(f"⚠️  web_search 도구 오류 (계속 진행): {', '.join(errors)}")
            return ""
        result = "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text").strip()
        if not result:
            print("⚠️  web_search 결과 텍스트 없음 (계속 진행)")
            return ""
        print(f"✅ web_search 완료 ({len(result)}자, requests={web_search_request_count(data)})")
        return result
    except Exception as exc:
        print(f"⚠️  web_search 실패/타임아웃 (재시도 없이 계속 진행): {exc}")
        return ""


def fetch_case_and_stat_research(topic, pubmed_query):
    """Collect anonymized Korean case-style material and domestic statistics via web_search."""
    print(
        "🔍 web_search 실사례/국내통계 자료 수집 중... "
        f"max_uses={CASE_RESEARCH_MAX_USES}, timeout={CASE_RESEARCH_TIMEOUT}s "
        f"(query: {pubmed_query})"
    )
    domain_hint = ", ".join(CASE_SOURCE_DOMAINS + STAT_SOURCE_DOMAINS)
    messages = [{"role": "user", "content":
        f"Search for Korean-language material about: {pubmed_query}\n\n"
        f"ONLY use these domains, ignore all other results: {domain_hint}\n\n"
        "From hidoc.co.kr / mdtoday.co.kr / k-health.com: find 1 realistic anonymized "
        "clinical-scenario style excerpt (e.g. '40대 직장인 B씨' style intro used in Korean health journalism). "
        "From government/hospital domains: find 1-2 Korean prevalence statistics with source name and year.\n\n"
        "Output format, plain text, no markdown:\n"
        "CASE: <one paraphrased sentence describing the scenario, anonymized, no real names>\n"
        "CASE_SOURCE: <site name>\n"
        "STAT: <one Korean statistic sentence>\n"
        "STAT_SOURCE: <site name>\n"
        "If nothing relevant found in the allowed domains, output exactly: NONE_FOUND"}]
    try:
        data = _call_claude_loop(
            messages,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": CASE_RESEARCH_MAX_USES,
            }],
            max_tokens=CASE_RESEARCH_MAX_TOKENS,
            model=CLAUDE_RESEARCH_MODEL,
            timeout=CASE_RESEARCH_TIMEOUT,
            max_turns=CASE_RESEARCH_MAX_TOOL_TURNS,
            stage="case_research",
        )
        errors = web_search_error_codes(data)
        if errors:
            print(f"⚠️  case_research web_search 도구 오류 (계속 진행): {', '.join(errors)}")
            return ""
        result = "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text").strip()
        if not result or result.strip() == "NONE_FOUND":
            print("ℹ️  case_research 관련 자료를 화이트리스트 도메인에서 찾지 못했습니다.")
            return ""
        print(f"✅ case_research 완료 ({len(result)}자, requests={web_search_request_count(data)})")
        return result
    except Exception as exc:
        print(f"⚠️  case_research 실패/타임아웃 (재시도 없이 계속 진행): {exc}")
        return ""


# ─────────────────────────────────────────────
# YouTube API 실데이터 인사이트 로더
# ─────────────────────────────────────────────

def load_youtube_guidance(topic):
    module_path = Path(__file__).resolve().parent.parent / "youtube" / "6_youtube_feedback.py"
    if not module_path.is_file():
        return {}
    try:
        spec = importlib.util.spec_from_file_location("brain50_youtube_feedback", module_path)
        if spec is None or spec.loader is None:
            return {}
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        guidance = module.prepare_content_guidance(
            topic,
            strictness=YOUTUBE_FEEDBACK_STRICTNESS,
            auto_sync=YOUTUBE_FEEDBACK_AUTO_SYNC,
        )
        with open(os.path.join(WORK_DIR, "youtube_guidance.json"), "w", encoding="utf-8") as f:
            json.dump(guidance, f, ensure_ascii=False, indent=2)
        return guidance
    except Exception as exc:
        print(f"⚠️  YouTube 실데이터 분석 실패, 기존 생성 흐름으로 계속합니다: {exc}")
        return {}


# ─────────────────────────────────────────────

CONTENT_INTENT_COMPARISON = "comparison"
COMPARISON_CONNECTORS = (
    " vs ", " VS ", "대 ", "와 ", "과 ", "랑 ", "하고 ", "또는 ",
    "비교", "중에", "중에서", "어느", "뭐가", "무엇이", "더 ",
)
DEFAULT_COMPARISON_CRITERIA = ["효과", "위험", "대상자", "실천 가능성"]
DEFAULT_COMPARISON_BEATS = [
    "시청자가 던진 비교 질문을 첫 부분에서 다시 확인한다",
    "비교 대상들을 모두 같은 기준으로 나란히 설명한다",
    "근거가 충분한 부분과 부족한 부분을 구분한다",
    "조건별로 어떤 선택이 더 맞는지 최종 답을 준다",
]
DEFAULT_EXPLANATION_BEATS = [
    "시청자의 질문이나 걱정을 첫 부분에서 다시 확인한다",
    "핵심 근거와 한계를 쉬운 말로 설명한다",
    "오늘 바로 할 수 있는 작은 실천을 제안한다",
    "마지막에 제목의 약속에 대한 최종 답을 준다",
]
GENERIC_FINAL_ANSWERS = (
    "술 종류에 따라 다르",
    "종류에 따라 다르",
    "사람마다 다르",
    "상황에 따라 다르",
)
INSUFFICIENT_EVIDENCE_STATUSES = {"insufficient", "limited", "none", "unsupported", "unknown"}
# A 숫자형 hook that opens on "대부분" has followed the label and skipped the
# point, so the vague quantifier disqualifies the scene even when a digit is
# present elsewhere in it.
VAGUE_QUANTIFIERS = (
    "많은", "대부분", "상당수", "적지 않은", "수많은", "대다수", "거의 모두", "절반가량",
)
# An open loop hung on a demonstrative promises nothing concrete enough to
# scroll for. Checked for every pattern, not just 호기심갭형, because every hook
# ends Scene 1 unresolved.
VAGUE_HOOK_ANCHORS = (
    "이 방법", "그 방법", "이것", "그것", "이 사실", "그 사실",
    "이 비밀", "그 비밀", "이 한 가지", "이 이유", "그 이유",
)


def bounded_research_context(abstracts="", web_research="", limit=3000):
    parts = []
    if abstracts:
        parts.append("[PubMed]\n" + str(abstracts).strip())
    if web_research:
        parts.append("[web_search]\n" + str(web_research).strip())
    text = "\n\n".join(part for part in parts if part.strip())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit("\n", 1)[0].strip() + "\n[근거 자료 일부 생략]"


def cleanup_comparison_target(piece):
    piece = normalize_keyword(piece)
    piece = re.split(r"\b(?:비교|차이|중에|중에서|어느|뭐가|무엇이|더|추천|나을까|좋을까)\b", piece, maxsplit=1)[0]
    piece = re.sub(r"^(?:추천|비교|차이|건강|50대|이상)\s+", "", piece).strip()
    piece = re.sub(r"\s+(?:추천|비교|차이|효능|효과|위험|부작용|건강|50대|이상)$", "", piece).strip()
    return piece.strip(" ,./-_|:;()[]{}")


def extract_named_comparison_targets(topic):
    text = normalize_keyword(topic)
    if not text:
        return []
    normalized = re.sub(r"\b(?:vs|VS|Vs)\b", " vs ", text)
    patterns = [
        r"(.+?)\s+vs\s+(.+)",
        r"(.+?)\s*(?:와|과|랑|하고|또는)\s*(.+)",
        r"(.+?)\s*대\s*(.+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, normalized)
        if not m:
            continue
        raw_targets = [cleanup_comparison_target(m.group(1)), cleanup_comparison_target(m.group(2))]
        targets = []
        for target in raw_targets:
            if target and target not in targets:
                targets.append(target)
        if len(targets) >= 2:
            return targets[:4]
    return []


def detect_content_intent(topic):
    targets = extract_named_comparison_targets(topic)
    if len(targets) >= 2:
        return CONTENT_INTENT_COMPARISON
    return CONTENT_INTENT_COMPARISON if any(hint in topic for hint in COMPARISON_CONNECTORS) else "explanation"


def normalize_strategy_contract(strategy, topic):
    strategy = dict(strategy or {})
    strategy["topic"] = topic
    intent_type = strategy.get("intent_type") or detect_content_intent(topic)
    had_explicit_targets = "comparison_targets" in strategy
    targets = strategy.get("comparison_targets") or []
    if not isinstance(targets, list):
        targets = [targets]
    targets = [normalize_keyword(target) for target in targets if normalize_keyword(target)]
    if intent_type == CONTENT_INTENT_COMPARISON and not had_explicit_targets and len(targets) < 2:
        targets = extract_named_comparison_targets(topic)
    strategy["intent_type"] = intent_type
    strategy["comparison_targets"] = targets
    if intent_type == CONTENT_INTENT_COMPARISON:
        joined = "와 ".join(targets[:2]) if len(targets) >= 2 else normalize_keyword(topic)
        default_question = f"{joined} 중 무엇을 선택해야 할까요?"
        default_requirement = "비교 대상별 차이와 조건별 선택 기준을 밝히고, 근거가 부족하면 단정하지 말 것"
        default_beats = list(DEFAULT_COMPARISON_BEATS)
        if not strategy.get("comparison_criteria"):
            strategy["comparison_criteria"] = list(DEFAULT_COMPARISON_CRITERIA)
    else:
        default_question = f"{normalize_keyword(topic)}에 대해 무엇을 알아야 할까요?"
        default_requirement = "제목에서 약속한 질문에 근거 기반 최종 답을 줄 것"
        default_beats = list(DEFAULT_EXPLANATION_BEATS)
        strategy.setdefault("comparison_criteria", [])
    strategy.setdefault("viewer_question", default_question)
    strategy.setdefault("answer_requirement", default_requirement)
    strategy.setdefault("evidence_status", "unknown")
    strategy.setdefault("final_answer", "")
    source_mix = strategy.get("source_mix") if isinstance(strategy.get("source_mix"), dict) else {}
    strategy["source_mix"] = {
        "case_summary": str(source_mix.get("case_summary") or ""),
        "case_source": str(source_mix.get("case_source") or ""),
        "stat_value": str(source_mix.get("stat_value") or ""),
        "stat_source": str(source_mix.get("stat_source") or ""),
    }
    strategy.setdefault("required_beats", default_beats)
    strategy.setdefault("title_promise", strategy.get("title") or strategy.get("core_message") or normalize_keyword(topic))
    return apply_story_type(strategy)


# Stage 1 — 전략 수립 (Haiku)
# ─────────────────────────────────────────────

def strategy_output_schema():
    string_fields = (
        "main_keyword", "search_intent", "hook_type", "title",
        "search_title_format", "core_message", "intent_type",
        "viewer_question", "answer_requirement", "evidence_status",
        "final_answer", "title_promise", "cta_next",
    )
    if USE_STORY_TYPES:
        # Required, not optional: a strategy without a genre would make Stage 2
        # fall back to the generic skeleton silently (design spec §5.2).
        string_fields = string_fields + ("story_type", "story_type_reason")
    properties = {field: {"type": "string"} for field in string_fields}
    if USE_STORY_TYPES:
        properties["story_type"] = {"type": "string", "enum": list(story_types.STORY_TYPES)}
    for field in ("sub_keywords", "comparison_targets", "comparison_criteria", "required_beats"):
        properties[field] = {"type": "array", "items": {"type": "string"}}
    properties["evidence_brief"] = {
        # Claude structured output (output_config.format: json_schema) rejects
        # "maxItems" on array types with HTTP 400 — the 6-item cap is enforced
        # only via the prompt instruction below, not the schema.
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "claim": {"type": "string"},
                "source": {"type": "string"},
                "year": {"type": "string"},
                "caveat": {"type": "string"},
            },
            "required": ["claim", "source", "year", "caveat"],
            "additionalProperties": False,
        },
    }
    properties["source_mix"] = {
        "type": "object",
        "properties": {
            "case_summary": {"type": "string"},
            "case_source": {"type": "string"},
            "stat_value": {"type": "string"},
            "stat_source": {"type": "string"},
        },
        "required": ["case_summary", "case_source", "stat_value", "stat_source"],
        "additionalProperties": False,
    }
    properties["frame_header"] = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "subtitle": {"type": "string"},
        },
        "required": ["title", "subtitle"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def local_strategy_fallback(topic, trend_context=None, reason=None):
    selected = ""
    if trend_context:
        selected = trend_context.get("selected", {}).get("keyword", "")
    keyword = normalize_keyword(selected or topic) or "오늘의 건강 습관"
    title = normalize_keyword(topic) or keyword
    strategy = normalize_strategy_contract({
        "topic": topic,
        "main_keyword": keyword[:12],
        "sub_keywords": [],
        "search_intent": "핵심 내용을 쉽게 확인하고 싶음",
        "hook_type": hook_types.CALLOUT,
        "title": title[:28],
        "search_title_format": "생활습관형",
        "core_message": "부담을 줄이는 작은 습관부터 시작하세요",
        "frame_header": {
            "title": clip_at_word_boundary(keyword, 10),
            "subtitle": "핵심만 쉽게 알려드립니다",
        },
        "cta_next": "다음 건강 습관",
        "intent_type": detect_content_intent(topic),
        "viewer_question": title,
        "answer_requirement": "핵심 포인트와 실천 방법을 분명히 설명할 것",
        "comparison_targets": extract_named_comparison_targets(topic),
        "comparison_criteria": list(DEFAULT_COMPARISON_CRITERIA),
        "evidence_status": "limited",
        "evidence_brief": [],
        "source_mix": {"case_summary": "", "case_source": "", "stat_value": "", "stat_source": ""},
        "final_answer": "근거를 바탕으로 부담을 줄이는 실천부터 시작하는 것이 좋습니다.",
        "required_beats": list(DEFAULT_EXPLANATION_BEATS),
        "title_promise": title,
    }, topic)
    strategy["strategy_source"] = "local_fallback"
    if reason:
        strategy["strategy_error"] = str(reason)[:700]
    with open(STRATEGY_PATH, "w", encoding="utf-8") as f:
        json.dump(strategy, f, ensure_ascii=False, indent=2)
    print("⚠️  Stage 1 모델 호출이 모두 실패해 로컬 기본 전략으로 Stage 2를 계속 진행합니다.")
    return strategy


def design_constraint_hint(content_design):
    """Carry goal-planner design decisions into Stage 1 as constraints.

    The objective planner picks format/hook/emotion curve to serve a metric goal.
    Stage 1 now writes the copy, so it has to respect those choices rather than
    re-deciding them.
    """
    design = content_design or {}
    if not isinstance(design, dict):
        return ""
    fields = (
        ("story_type", "스토리 타입"),
        ("format_type", "콘텐츠 형식"),
        ("hook_type", "훅 유형"),
        ("topic_family", "주제 계열"),
        ("angle", "접근 각도"),
        ("series_key", "시리즈 키"),
    )
    lines = [
        f"- {label}: {design[key]}"
        for key, label in fields
        if str(design.get(key) or "").strip()
    ]
    curve = design.get("emotion_curve")
    if isinstance(curve, list) and curve:
        lines.append(f"- 감정 곡선: {' → '.join(str(item) for item in curve)}")
    if not lines:
        return ""
    return (
        "\n\n[확정된 콘텐츠 설계 — 목표 기반 기획 단계에서 결정됨]\n"
        + "\n".join(lines)
        + "\nhook_type과 형식은 위 설계를 따르고, 카피(제목·키워드·훅 문구)만 새로 작성하세요."
    )


def merge_planning_contract(strategy, pre_strategy):
    """Keep planning-stage decisions after Stage 1 rewrites the copy.

    Stage 1 owns the wording and sets `strategy_source="claude"`, which is what
    enables the cheaper `evidence_brief` digest in Stage 2. The goal planner's
    design, objective and audit fields have to survive that overwrite.
    """
    merged = dict(strategy)
    for key in ("content_design", "objective", "planning"):
        value = pre_strategy.get(key)
        if value:
            merged[key] = value
    planning_source = pre_strategy.get("strategy_source")
    if planning_source:
        merged["planning_source"] = planning_source
    design = pre_strategy.get("content_design") or {}
    if isinstance(design, dict) and str(design.get("hook_type") or "").strip():
        merged["hook_type"] = design["hook_type"]
    # The planner's genre outranks Stage 1's echo of it: the mix quota was spent
    # on that choice when the candidate was selected, so a model that ignored
    # the "변경 금지" instruction must not silently rewrite the distribution.
    planned_story_type = story_types.normalize((design or {}).get("story_type"))
    if USE_STORY_TYPES and planned_story_type:
        if story_types.normalize(merged.get("story_type")) not in (None, planned_story_type):
            print(
                f"⚠️  Stage 1이 story_type을 '{merged.get('story_type')}'으로 바꿨지만 "
                f"기획 단계 확정값 '{planned_story_type}'을 유지합니다."
            )
        merged["story_type"] = planned_story_type
    return merged


def plan_strategy(
    topic,
    abstracts="",
    web_research="",
    case_research="",
    trend_context=None,
    youtube_guidance="",
    content_design=None,
):
    """
    Haiku로 빠르게 콘텐츠 전략(검색 키워드·제목·훅 유형·핵심 메시지)을 결정한다.
    Stage 2 대본 작성 전 뼈대를 확정하는 역할.
    """
    import requests

    trend_hint = ""
    if trend_context:
        kw = trend_context.get("selected", {}).get("keyword", "")
        if kw:
            trend_hint = f"\n트렌드 참고: {kw}"

    research_context = bounded_research_context(abstracts, web_research)
    if case_research:
        research_context = (research_context + "\n\n[case_and_stat_research]\n" + case_research).strip()
    research_hint = f"\n\n[근거 자료 요약]\n{research_context}" if research_context else ""
    channel_hint = f"\n\n{youtube_guidance}" if youtube_guidance else ""
    design_hint = design_constraint_hint(content_design)
    decided_story_type = strategy_story_type({"content_design": content_design or {}})
    story_hint = story_type_directive(decided_story_type, bool(decided_story_type))
    # Stage 1 picks the hook pattern, so the concreteness rules have to reach it
    # too — otherwise a 숫자형 job gets a title with no real number in it.
    hook_rule = (
        "hook_type    : " + " / ".join(hook_types.HOOK_PATTERNS) + " 중 하나\n"
        + hook_types.prompt_block() + "\n"
    )
    story_rule = ""
    story_json_fields = ""
    if USE_STORY_TYPES:
        story_rule = (
            "story_type   : " + " / ".join(story_types.STORY_TYPES) + " 중 하나 (위 지시 참고)\n"
            "story_type_reason: story_type을 그렇게 정한 이유 한 줄 (이미 확정된 경우 '기획 단계 확정')\n"
        )
        story_json_fields = ',\n  "story_type": "",\n  "story_type_reason": ""'

    prompt = f"""{creative_dna_block()}주제: {topic}{trend_hint}{research_hint}{channel_hint}{design_hint}{story_hint}

이 주제로 50대 이상을 위한 YouTube Shorts 콘텐츠 전략을 수립하세요.

채널 실데이터가 있으면 제목·핵심어·차별화 방향을 정할 때 반영하되, 작은 표본을 성공 공식으로 단정하지 마세요.
의학적 사실과 최종 결론은 채널 성과가 아니라 검증된 근거 자료를 우선하세요.

[규칙]
main_keyword : YouTube에서 실제 검색할 핵심 키워드 (공백 포함 12자 이내)
sub_keywords : 연관 검색어 2~3개 (배열)
search_intent: 이 키워드를 검색하는 사람의 상황/걱정 (20자 이내)
{hook_rule}{story_rule}title        : 영상 본문과 훅을 자연스럽게 대표하는 한국어 제목 (15~28자 권장)
               - 사용자가 입력한 주제문을 그대로 복사하거나 어순만 바꾸지 말 것
               - main_keyword는 가능하면 앞쪽에 넣되, 억지스럽거나 기계적인 제목 금지
               - 실제 영상에서 밝혀지는 긴장/반전/해결 약속이 제목에 드러나야 함
               - 과장·공포 조장 대신 "궁금해서 누르게 되는" 생활형 문장으로 작성
               - 제목에 # 해시태그를 넣지 말 것
               - 답을 제목에서 확정해 말하지 말 것. 무엇을 다루는지만 알리고 결론은 본문에서 밝힐 것
search_title_format: 제목 성격 (질문형/비교형/체크리스트형/생활습관형/반전형/공감형 중 하나)
core_message : 시청자가 이 영상에서 가져갈 딱 한 문장 (30자 이내)
frame_header : 상단 프레임용 2줄 훅 후보. 대본 맥락을 압축한 추상적이지만 이해 가능한 문구
               - title 4~10자 권장, subtitle 10~22자 권장
               - 두 줄 모두 의미가 끊기지 않는 완결된 구문으로 작성할 것. 글자 수를 맞추려고 문장을
                 중간에서 끊거나 조사를 잘라내지 말 것. 렌더러가 길이에 맞춰 글자 크기를 자동으로
                 줄이므로, 짧게 만드는 것보다 문맥이 완결되는 것이 훨씬 중요하다.
               - title은 subtitle보다 짧게 잡아 위는 짧고 아래는 긴 삼각형 구도로 만들 것
               - 사용자가 입력한 주제어를 그대로 복사하지 말 것
               - 호기심·반전·해결 약속이 느껴지게 작성
cta_next     : 다음 영상 예고 주제 (파생 주제, 20자 이내)
intent_type  : comparison / explanation 중 하나. 두 대상을 비교하는 주제면 반드시 comparison
viewer_question: 시청자가 실제로 묻는 질문을 한 문장으로 명시
answer_requirement: 최종 대본이 반드시 답해야 하는 조건
comparison_targets: 비교형이면 사용자가 요청한 비교 대상을 2개 이상 보존
comparison_criteria: 비교 기준 2~4개
evidence_status: sufficient / limited / insufficient 중 하나
evidence_brief: 근거 자료에서 대본에 필요한 사실 0~6개. 각 항목은 claim, source, year, caveat를 포함하고 자료가 없으면 빈 배열
               - [PubMed 서지 정보] 블록이 있으면 source/year는 반드시 그 목록의 값을 그대로 사용할 것 (추측 금지)
               - 그 블록이 없으면 근거 자료 텍스트에서 실제로 확인되는 값만 쓰고, 불확실하면 빈 문자열로 둘 것
source_mix   : [case_and_stat_research] 블록이 있으면 그 내용을 바탕으로 채우고, 없으면 모든 값을 빈 문자열로 둘 것.
               case_summary는 반드시 완전히 새로운 문장으로 재구성하고 실명·특정 가능 정보는 제거할 것. 절대 원문 문장을 그대로 옮기지 말 것.
final_answer : 현재 근거로 가능한 최종 답의 초안. 단정 불가 시 그 한계를 포함
required_beats: 대본에 반드시 포함할 전개 비트 배열
title_promise: 제목이 시청자에게 약속하는 답변

JSON만 출력. 설명·주석·마크다운 없이.

{{
  "main_keyword": "",
  "sub_keywords": [],
  "search_intent": "",
  "hook_type": "",
  "title": "",
  "search_title_format": "",
  "core_message": "",
  "frame_header": {{"title": "", "subtitle": ""}},
  "intent_type": "",
  "viewer_question": "",
  "answer_requirement": "",
  "comparison_targets": [],
  "comparison_criteria": [],
  "evidence_status": "",
  "evidence_brief": [{{"claim": "", "source": "", "year": "", "caveat": ""}}],
  "final_answer": "",
  "required_beats": [],
  "title_promise": "",
  "cta_next": ""{story_json_fields}
}}"""

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return local_strategy_fallback(topic, trend_context, "ANTHROPIC_API_KEY가 없습니다.")

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "max_tokens": CLAUDE_STRATEGY_MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
        "output_config": {
            "format": {
                "type": "json_schema",
                "schema": strategy_output_schema(),
            }
        },
    }

    last_err = None
    for model in strategy_model_candidates():
        payload["model"] = model
        try:
            enforce_claude_budget()
            print(f"📋 Stage 1: 전략 수립 중 (model={model}, max_tokens={CLAUDE_STRATEGY_MAX_TOKENS})...")
            res = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=30,
            )
            if res.status_code >= 400:
                try:
                    failed_response = res.json()
                except Exception:
                    failed_response = {}
                failed_response["_request_id"] = res.headers.get("request-id")
                record_claude_usage("strategy", model, failed_response, success=False)
                last_err = RuntimeError(describe_http_error(res))
                print(f"  ⚠️  실패, 다음 모델로 전환: {last_err}")
                continue

            data = res.json()
            data["_request_id"] = (
                res.headers.get("request-id")
                or res.headers.get("anthropic-request-id")
                or res.headers.get("x-request-id")
            )
            record_claude_usage("strategy", model, data)
            stop_reason = data.get("stop_reason")
            if stop_reason != "end_turn":
                last_err = RuntimeError(f"Stage 1 비정상 종료: stop_reason={stop_reason}")
                print(f"  ⚠️  실패, 다음 모델로 전환: {last_err}")
                continue

            raw = data["content"][0]["text"].strip()
            strategy = normalize_strategy_contract(json.loads(raw), topic)
            strategy["strategy_source"] = "claude"
            print(f"  ✅ main_keyword    : {strategy.get('main_keyword')}")
            print(f"  ✅ title           : {strategy.get('title')}")
            print(f"  ✅ hook_type       : {strategy.get('hook_type')}")
            if USE_STORY_TYPES:
                print(f"  ✅ story_type      : {strategy.get('story_type')} / {strategy.get('format_type')}")
            print(f"  ✅ search_format   : {strategy.get('search_title_format')}")
            print(f"  ✅ core_message    : {strategy.get('core_message')}")

            with open(STRATEGY_PATH, "w", encoding="utf-8") as f:
                json.dump(strategy, f, ensure_ascii=False, indent=2)
            return strategy
        except Exception as exc:
            last_err = exc
            print(f"  ⚠️  실패, 다음 모델로 전환: {exc}")

    return local_strategy_fallback(topic, trend_context, last_err)


# ─────────────────────────────────────────────
def stage2_research_context(strategy, abstracts="", web_research=""):
    """Prefer the cheaper Stage 1 evidence digest; retain raw-context compatibility fallback."""
    brief = strategy.get("evidence_brief") or []
    valid = [item for item in brief if isinstance(item, dict) and str(item.get("claim") or "").strip()]
    if strategy.get("strategy_source") == "claude" and valid:
        return "[검증된 근거 요약]\n" + json.dumps(valid, ensure_ascii=False, separators=(",", ":"))
    parts = []
    if abstracts:
        parts.append("[PubMed 초록]\n" + str(abstracts).strip())
    if web_research:
        parts.append("[최신 영문 연구]\n" + str(web_research).strip())
    return "\n\n".join(parts)


def adaptive_research_policy(pubmed_status, strategy=None):
    """Choose expensive research only after the final topic/format is known."""
    strategy = strategy or {}
    content_design = strategy.get("content_design") or {}
    format_type = str(content_design.get("format_type") or strategy.get("format_type") or "")
    pubmed_sufficient = bool(pubmed_status and pubmed_status.get("status") == "ok")
    if RESEARCH_MODE != "adaptive":
        return {"web": True, "case": True, "reason": "configured_full"}
    run_web = (not pubmed_sufficient) or format_type == "연구발견형"
    run_case = format_type == "사례추적형"
    return {
        "web": run_web,
        "case": run_case,
        "reason": "pubmed_sufficient" if pubmed_sufficient else "pubmed_insufficient",
    }


def narrative_template(strategy):
    design = strategy.get("content_design") or {}
    format_type = str(design.get("format_type") or strategy.get("format_type") or "오해반전형")
    emotion_curve = design.get("emotion_curve") or ["공감", "이해", "안심", "행동"]
    templates = {
        "오해반전형": "익숙한 오해를 먼저 짚고, 근거로 반전한 뒤, 안전한 실천으로 마무리하세요.",
        "자가진단형": "일상 신호를 차례로 점검하되 진단처럼 단정하지 말고 상담 기준과 실천을 제시하세요.",
        "사례추적형": "한 생활 장면을 따라 원인 후보를 풀고, 사례를 일반화하지 않은 채 실천으로 연결하세요.",
        "비교형": "두 대상을 같은 기준으로 나란히 비교하고, 조건별 선택 기준을 분명히 답하세요.",
        "연구발견형": "연구가 새롭게 보여준 점, 한계, 일상에서의 의미 순서로 설명하세요.",
        "행동챌린지형": "오늘 할 한 가지 행동을 초반에 약속하고, 방법과 확인 기준을 짧게 제시하세요.",
    }
    return format_type, [str(item) for item in emotion_curve], templates.get(
        format_type, templates["오해반전형"]
    )

# Stage 2 — 프롬프트 빌더
# ─────────────────────────────────────────────

def story_type_block(strategy):
    """The Stage 2 skeleton: shared story rules + this genre's role sequence.

    Returns "" when the feature is off or the template files are missing, in
    which case Stage 2 falls back to the pre-story_type emotion-curve prompt
    below. Where the two overlap the story_type sequence wins (spec §6.3), so
    this block is placed after the emotion curve in the assembled prompt.
    """
    if not USE_STORY_TYPES:
        return ""
    story_type = strategy_story_type(strategy)
    if not story_type:
        return ""
    template = load_story_template(story_type)
    if not template:
        return ""
    sequence = " → ".join(story_types.role_sequence(story_type))
    parts = [
        "\n[스토리 타입 — 이 영상의 뼈대. 아래 감정 곡선보다 이 순서를 우선하세요]",
        f"story_type: {story_types.describe(story_type)}",
        f"role 시퀀스: {sequence}",
    ]
    common = load_story_common_template()
    if common:
        parts.append(common)
    parts.append(template)
    return "\n\n".join(parts) + "\n"


def scene_output_spec():
    """The `scenes[]` shape asked of Stage 2, and the rules for filling it.

    Stage 2 writes words only. What each scene *shows* — `visual` and the
    English `visual_query` — is planned by scene_visuals.py after the
    approval gate, because asking for it here fixes the plan to a draft the
    operator has not read yet, let alone edited.

    With story types on the scene still carries `role`: the genre's beat is
    an input to how the sentence is written, not a description of it.
    """
    # Braces are single here: these strings are interpolated *into* the prompt
    # f-string as values, so they are never re-parsed for placeholders.
    if not USE_STORY_TYPES:
        return "", '{"text": "한국어 장면 텍스트"}'
    rules = (
        "\n7. scenes[].role: 위 role 시퀀스의 이름을 그대로 쓰세요. hook으로 시작하고 cta로 끝나야 합니다.\n"
        "   - 분량 때문에 role을 건너뛰지 마세요. 나눠야 하면 같은 role 이름을 가진 Scene 두 개로 쪼개세요.\n"
    )
    return rules, '{"text": "한국어 장면 텍스트", "role": "hook"}'


def pace_instruction():
    if ATEMPO >= 1.2:
        return "매우 빠르고 에너지 있는 말투. 짧은 문장, 빠른 리듬."
    if ATEMPO >= 1.1:
        return "조금 빠른 대화체. 문장은 짧게, 압축해서 전달."
    return "따뜻하고 여유 있는 대화체. 자연스러운 쉼표와 호흡."


def build_prompt(strategy, abstracts, trend_context=None, web_research="", youtube_guidance=""):
    main_keyword   = strategy.get("main_keyword", "")
    # The single point where a legacy hook label on disk is upgraded before it
    # reaches Stage 2, so old work dirs resumed after deploy still render a
    # pattern the prompt actually describes.
    hook_type      = hook_types.normalize(strategy.get("hook_type")) or hook_types.CALLOUT
    title          = strategy.get("title", "")
    core_message   = strategy.get("core_message", "")
    search_intent  = strategy.get("search_intent", "")
    cta_next       = strategy.get("cta_next", "")
    topic          = strategy.get("topic", main_keyword)
    search_format  = strategy.get("search_title_format", "")
    objective = strategy.get("objective") or {}
    format_type, emotion_curve, narrative_instruction = narrative_template(strategy)

    strategy = normalize_strategy_contract(strategy, topic)
    contract = {
        "intent_type": strategy.get("intent_type"),
        "viewer_question": strategy.get("viewer_question"),
        "answer_requirement": strategy.get("answer_requirement"),
        "comparison_targets": strategy.get("comparison_targets", []),
        "comparison_criteria": strategy.get("comparison_criteria", []),
        "evidence_status": strategy.get("evidence_status"),
        "final_answer": strategy.get("final_answer"),
        "required_beats": strategy.get("required_beats", []),
        "title_promise": strategy.get("title_promise"),
    }
    contract_block = json.dumps(contract, ensure_ascii=False, indent=2)
    source_mix = strategy.get("source_mix") or {}
    case_summary = str(source_mix.get("case_summary") or "").strip()
    case_source = str(source_mix.get("case_source") or "").strip()
    stat_value = str(source_mix.get("stat_value") or "").strip()
    stat_source = str(source_mix.get("stat_source") or "").strip()
    source_mix_instruction = ""
    if case_summary or stat_value:
        lines = ["\n[실사례·통계 활용 지침 — 1단계(공감) 구간에 자연스럽게 녹여 쓸 것]"]
        if case_summary:
            lines.append(
                f"- 참고 사례(반드시 재구성, 원문 그대로 인용 금지, 익명 유지): {case_summary}"
                f"{f' (출처: {case_source})' if case_source else ''}"
            )
        if stat_value:
            lines.append(
                f"- 참고 통계(출처를 자연스러운 한 문장 안에서 언급): {stat_value}"
                f"{f' (출처: {stat_source})' if stat_source else ''}"
            )
        lines.append("- 두 항목 모두 15어절 이상 원문 그대로 옮기지 말고 완전히 새 문장으로 쓸 것.")
        source_mix_instruction = "\n".join(lines)
    beats_block = "\n".join(f"- {beat}" for beat in contract.get("required_beats", []) if beat)
    comparison_instruction = ""
    if contract.get("intent_type") == CONTENT_INTENT_COMPARISON:
        target_list = ", ".join(contract.get("comparison_targets") or []) or "비교 대상 미정"
        criteria_list = ", ".join(contract.get("comparison_criteria") or []) or "효과, 위험, 대상자"
        comparison_instruction = (
            f"\n[비교형 필수 조건]\n"
            f"- 비교 대상({target_list})을 모두 본문과 final_answer에 직접 언급하세요.\n"
            f"- 같은 비교 기준({criteria_list})으로 설명하고, 한쪽을 승자로 단정하려면 근거가 충분해야 합니다.\n"
            "- 근거가 부족하면 '현재 근거로는 단정하기 어렵다'고 말하고 조건별 선택 기준을 답하세요.\n"
        )

    # ── 트렌드 블록
    trend_block = ""
    if trend_context:
        candidates = ", ".join(i["keyword"] for i in trend_context.get("candidates", []))
        trend_block = (f"\n트렌드 참고: 씨드={trend_context.get('seed','')}, "
                       f"선택={trend_context.get('selected',{}).get('keyword', topic)}, "
                       f"후보={candidates}\n")

    research_block = stage2_research_context(strategy, abstracts, web_research)

    # ── YouTube 채널 실데이터 블록
    youtube_block = ""
    if youtube_guidance:
        youtube_block = (
            f"\n{youtube_guidance}\n"
            "※ 채널 성과는 구성·표현의 방향에만 사용하고 의학적 사실은 연구 근거를 우선하세요.\n"
        )

    story_block = story_type_block(strategy)
    hook_block = hook_types.prompt_block(hook_type)
    scene_rules, scene_shape = scene_output_spec()

    prompt_min_chars = max(1, int(total_chars * PROMPT_MIN_LENGTH_RATIO))
    prompt_max_chars = max(prompt_min_chars, int(total_chars * PROMPT_MAX_LENGTH_RATIO))
    hard_max_chars = max(prompt_max_chars, int(total_chars * MAX_SCRIPT_LENGTH_RATIO))

    return f"""{creative_dna_block()}아래는 '{topic}'와 관련한 연구 자료와 콘텐츠 전략입니다. 

당신은 50대 이상 시청자들의 일상적 고민을 진심으로 경청하고, 불안감을 따뜻하게 보듬어주는 '다정한 동네 주치의'이자 스토리텔러입니다. 정보를 다그치듯 나열하지 말고, 자녀나 오랜 친구가 조곤조곤 챙겨주듯 다정한 이야기로 풀어내세요.

=== 연구 자료 및 전략 데이터 ===
{research_block}
{youtube_block}{trend_block}
[콘텐츠 전략 (Stage 1 결과)]
main_keyword       : {main_keyword}
검색 의도          : {search_intent}
제목 후보          : {title}
훅(Hook) 유형      : {hook_type}
핵심 메시지        : {core_message} (이 메시지가 영상 전체의 따뜻한 결론이 되어야 합니다)

[이번 영상의 목표]
{objective.get('type', 'balanced')} (결정={objective.get('decision', '기존 제작')}, 확신도={objective.get('confidence', '-')})

[콘텐츠 형식]
{format_type}

[감정 곡선]
{' → '.join(emotion_curve)}

[형식별 서사 지침]
{narrative_instruction}
{story_block}
[콘텐츠 계약]
{contract_block}
{comparison_instruction}{source_mix_instruction}===

위 자료를 바탕으로 유튜브 쇼츠 대본을 작성해 주세요. 지정한 형식과 감정 곡선을 따르되, 50대 이상 시청자를 불필요하게 겁주지 말고 마지막에는 안전한 실천 또는 판단 기준을 남기세요.

[반드시 포함할 전개 비트]
{beats_block}

─── 📖 따뜻한 스토리텔링 흐름 ───
[1단계: 3초 안에 스크롤을 멈추게 하는 강렬한 훅 → 공감으로 착지]
- Scene 1(첫 문장)은 무조건 스크롤을 멈추게 만드는 강력한 한 방이어야 합니다. 밋밋하게 시작하면 실패입니다.
  아래 6개 패턴 중 이 영상에 배정된 훅 유형({hook_type})을 우선 쓰되, 주제에 더 맞는 패턴이 있으면 그것을 고르세요.
{hook_block}
- 훅 문장은 단정적이고 힘 있는 어조로 쓰세요. "~할 수도 있어요" 같은 완곡체보다
  "~입니다", "~하고 계십니다"처럼 단호하게 끊어 쓰는 것이 몰입도를 높입니다.
- 단, 다음은 절대 금지합니다: 근거 없이 특정 질병을 확정 진단하는 표현("~하면 치매 걸립니다", "~하면 죽습니다" 등
  인과를 100% 확정하는 문장), 실제 존재하지 않는 통계 창작, 특정 병원·약품·시술 비방.
  → 자극은 "궁금증과 경각심을 최대치로 끌어올리는 것"까지이며, "근거 없는 공포 조장"은 다른 문제입니다.
- Scene 1은 답을 주지 않은 채로 끝내세요. 결론과 핵심 메시지를 Scene 1에 넣지 말고
  "그런데 진짜 이유는 따로 있습니다", "여기서 대부분이 반대로 알고 계십니다"처럼 미완결 어구로 마감하세요.
- Scene 2는 공감 구간이 아니라 긴장을 유지하는 구간입니다. 훅에서 던진 것을 더 구체적인 장면·상황으로 좁히세요.
  "혹시 이런 적 있으셨나요?" 같은 일반적인 공감 질문으로 Scene 2를 시작하지 마세요.
- 다정한 공감 착지는 Scene 3 이후에 두세요.

[2단계: 다정한 눈높이 설명]
- 어려운 의학 수치나 연구 결과를 지식 자랑하듯 설명하지 마세요. 일상적인 비유(예: 오래 켜둔 전구, 가을철 마른 나무 등)를 들어 쉽게 풀어주세요.
- 시청자가 무안하거나 죄책감을 느끼지 않게 하는 것이 핵심입니다. 

[3단계: 안도감을 주는 실천과 다짐]
- 겁을 주거나 위협하며 끝내지 마세요. 오늘 당장, 당장 힘들이지 않고 시작할 수 있는 '구체적이고 작은 행동 팁 1가지'(시간, 양, 횟수 명시)를 선물하듯 제안하세요.
- 마지막에는 핵심 메시지인 "{core_message}"를 건네며 안도감을 주는 멘트를 넣으세요.
- 다음 주제인 "{cta_next}"를 예고하며 인사를 건네며 마무리하세요.
- Scene 8~9 중 한 곳에 "아침형이세요, 저녁형이세요?"처럼 단어로 답이 끝나는 양자택일 댓글 질문을 1개 넣으세요. 개방형 질문은 쓰지 마세요.

─── 📝 작성 및 포맷 규칙 ───
1. 분량 및 씬: scenes의 text에 포함된 한글 음절만 세어 총 {prompt_min_chars}~{prompt_max_chars}자로 작성하세요. 공백, 숫자, 영문, 문장부호는 글자 수에서 제외합니다. 절대 상한은 {hard_max_chars}자이며 이를 넘기지 마세요.
   - 훅, 핵심 설명, 필수 전개 비트, 구체적 실천, final_answer로 이어지는 결론을 유지하면서 반복 설명과 긴 비유를 압축하세요.
   - 장면 수는 절대 조건이 아닙니다. 문맥과 호흡에 맞게 보통 5~10개 사이에서 유연하게 정하고, 각 장면은 1~2개의 짧은 문장으로 작성하세요.
2. 문체 및 톤: {pace_instruction()} 철저하게 존댓말을 사용하며, 다정하고 친근한 말투를 유지하세요.
3. 전문용어 순화: 어려운 용어는 한 장면에 1개 이하로 제한하고, 무조건 쉬운 말로 풀어서 쓰세요. 
   - 예: 인지기능 -> '기억하고 판단하는 힘', 염증 반응 -> '몸속 경보가 켜진 상태'
4. TTS 발음 최적화(매우 중요):
   - %, ~, 화살표 등 모든 기호는 한글 문장으로 완벽히 풀어 쓰세요. (예: 30% -> 30퍼센트, 3~5배 -> 3에서 5배)
   - 숫자 뒤의 단위는 공백 없이 붙여 쓰세요. 영어 약어(LDL, DNA 등)는 그대로 유지합니다.
5. 화면에 무엇이 보일지는 여기서 정하지 마세요. 대본이 확정된 뒤 별도 단계가 최종 문장을 보고 계획합니다.
6. frame_header: 상단에 들어갈 2줄 훅. 영상 전체 훅 강도와 통일감을 갖도록 강하게 쓰세요.
   - title(대제목): 공백 포함 4~10자 권장. 무난한 명사형 나열 대신 임팩트 있는 단정형·경고형·지목형 어구를 우선하세요.
     예: "이거 하셨다면", "몰랐다면 위험", "매일 이러셨죠" (단, 근거 없는 질병 확정 표현은 금지)
   - subtitle(소제목): 공백 포함 10~22자 권장. 궁금증이나 경각심을 증폭시켜 다음 내용을 보고 싶게 만드는 문장으로 마무리하세요.
   - 두 줄 모두 의미가 끊기지 않는 완결된 구문이어야 합니다. 글자 수를 맞추려고 문장을 중간에서
     끊거나 조사를 잘라내지 마세요. 렌더러가 길이에 맞춰 글자 크기를 자동으로 줄이고 필요하면
     줄바꿈하므로, 짧게 만드는 것보다 문맥이 완결되는 것이 훨씬 중요합니다.
   - 사용자가 준 단어를 그대로 복사하지 말고, 이 영상 고유의 훅으로 재창작하세요.
※ AI 티 피하기: 문장이 번역한 것처럼 딱딱하게 읽히지 않도록 아래를 피하세요.
   - "~에 대해/~를 통해/~에 있어서" 조사 남발 금지 (예: "이 문제에 대해 설명하면" → "이 문제를 설명하면")
   - 이중 피동 "~되어진다/~지게 된다" 금지 (예: "판단되어진다" → "판단됩니다")
   - "~에 의해" 피동 대신 행위자를 주어로 (예: "몸에 의해 만들어진다" → "몸이 만든다")
   - "결론적으로", "시사하는 바가 크다", "주목할 만하다" 같은 AI 관용구 금지
   - "크게 세 가지로 나눌 수 있는데요" 같은 열거 도입구 없이 바로 본론으로
   - "혁신적", "획기적", "압도적", "전례 없는" 같은 과장 어휘 반복 금지
   - "~할 가능성이 있을 수 있어요"처럼 완곡 표현을 두 번 겹쳐 쓰지 말고 하나만 남겨 단언
   - 문장 앞에 "또한", "따라서", "즉", "나아가" 같은 접속사를 연달아 붙이지 마세요
   - "~것입니다", "~것이에요"로 문장을 연달아 끝맺지 말고 단정하는 말투로 마무리
   - 같은 종결어미를 네 문장 이상 이어 쓰지 말고 어미를 섞어 자연스러운 리듬을 만드세요
{scene_rules}
반드시 아래 JSON 객체 포맷으로만 출력하세요. 마크다운이나 추가 설명은 절대 넣지 마세요.

{{
  "title": "본문과 훅을 자연스럽게 대표하는 최종 제목",
  "hook_type": "{hook_type}",
  "main_keyword": "{main_keyword}",
  "search_title_format": "{search_format}",
  "hashtags": "#태그1 #태그2 #태그3",
  "frame_header": {{"title": "대제목", "subtitle": "소제목"}},
  "description": "설명란 인트로 텍스트",
  "final_answer": "제목과 시청자 질문에 대한 한 문장 최종 답",
  "hook_open_loop": "Scene 1 끝에서 아직 답하지 않고 남긴 질문 한 문장. 감추는 대상을 '이 방법' 같은 지시대명사가 아니라 구체 명사로 지목할 것",
  "promise_fulfilled": true,
  "evidence_limit": "근거가 부족하거나 비교가 불가한 지점",
  "scenes": [
    {scene_shape}
  ]
}}"""


# ─────────────────────────────────────────────
# Stage 2 — Claude 호출
# ─────────────────────────────────────────────

def call_claude(prompt):
    payload = {
        "model": CLAUDE_SCRIPT_MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    return call_claude_api(payload, CLAUDE_TIMEOUT, "Claude 대본 생성", "script")


def result_char_count(result):
    return sum(korean_char_count(str(scene.get("text", ""))) for scene in (result.get("scenes") or []) if isinstance(scene, dict))


def build_compression_prompt(result, strategy):
    target_min = max(1, int(total_chars * PROMPT_MIN_LENGTH_RATIO))
    target_max = max(target_min, int(total_chars * PROMPT_MAX_LENGTH_RATIO))
    hard_max = max(target_max, int(total_chars * MAX_SCRIPT_LENGTH_RATIO))
    return f"""아래 유튜브 쇼츠 JSON 대본을 문맥과 사실관계를 유지한 채 압축하세요.

[압축 규칙]
- scenes의 text에 포함된 한글 음절만 세어 총 {target_min}~{target_max}자로 맞추세요.
- 어떠한 경우에도 {hard_max}자를 넘기지 마세요. 공백, 숫자, 영문, 문장부호는 계산에서 제외합니다.
- 제목의 약속, 훅, 핵심 설명, 필수 전개 비트, 구체적 실천, final_answer와 결론을 보존하세요.
- 새로운 의학 주장, 수치, 근거를 추가하지 마세요.
- 반복, 장황한 비유, 중복 CTA를 먼저 줄이세요. 문장 중간을 기계적으로 자르지 마세요.
- 장면 수는 고정하지 말고 자연스러운 문맥에 맞게 합치거나 줄이세요.
- 기존 JSON 필드와 유효한 JSON 객체 형식을 유지하고 JSON만 출력하세요.

[콘텐츠 계약]
{json.dumps(normalize_strategy_contract(strategy, strategy.get('topic', '')), ensure_ascii=False)}

[원본 JSON]
{json.dumps(result, ensure_ascii=False)}"""


def revise_overlong_script(result, strategy):
    original_count = result_char_count(result)
    hard_max = int(total_chars * MAX_SCRIPT_LENGTH_RATIO)
    if original_count <= hard_max:
        return result
    with open(os.path.join(WORK_DIR, "script_before_compression.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    prompt = build_compression_prompt(result, strategy)
    with open(os.path.join(WORK_DIR, "claude_compression_prompt.txt"), "w", encoding="utf-8") as f:
        f.write(prompt)
    payload = {"model": CLAUDE_STRATEGY_MODEL, "max_tokens": MAX_TOKENS, "messages": [{"role": "user", "content": prompt}]}
    print(f"대본이 허용 상한을 초과해 Haiku 압축 보정을 1회 수행합니다: {original_count}자 > {hard_max}자")
    response = call_claude_api(payload, CLAUDE_TIMEOUT, "Claude 대본 압축", "script_compression")
    revised = parse_claude_json(response, raw_filename="raw_compression_response.txt")
    print(f"Haiku 압축 보정 결과: {original_count}자 -> {result_char_count(revised)}자")
    return revised

def parse_claude_json(response, raw_filename="raw_response.txt"):
    print(f"  stop_reason: {response['stop_reason']}, usage: {response['usage']}")
    raw = response["content"][0]["text"]
    with open(os.path.join(WORK_DIR, raw_filename), "w", encoding="utf-8") as f:
        f.write(raw)
    if response["stop_reason"] == "max_tokens":
        raise Exception("Claude 출력 잘림. MAX_TOKENS를 높이세요.")
    raw = re.sub(r"^```(?:json)?", "", raw.strip()).rstrip("`").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        try:
            # Claude가 JSON 뒤에 부연 설명을 덧붙이는 경우, 앞쪽의 유효한 JSON 객체만 추출
            obj, _ = json.JSONDecoder().raw_decode(raw)
            return obj
        except json.JSONDecodeError:
            pass
        print("===== Claude Raw ====="); print(raw); print("======================")
        raise Exception(f"JSON 파싱 실패: {e}") from e


# ─────────────────────────────────────────────
# 트리밍 & 출력
# ─────────────────────────────────────────────

def korean_char_count(text):
    return len(re.sub(r"[^\uAC00-\uD7A3]", "", text))

def normalize_frame_header(result, strategy):
    raw = result.get("frame_header") or strategy.get("frame_header") or {}
    if not isinstance(raw, dict):
        raw = {}
    title = re.sub(r"\s+", " ", str(raw.get("title") or "").strip())
    subtitle = re.sub(r"\s+", " ", str(raw.get("subtitle") or "").strip())

    if not title:
        fallback_title = result.get("title") or strategy.get("title") or "브레인피프티"
        title = re.sub(r"\s+", " ", str(fallback_title).strip())
    if not subtitle:
        subtitle = "오늘의 뇌건강"

    # These caps only contain abnormal model output; normal overflow is preserved
    # for auto-shrink and word wrapping in frame_style.py. Clipping happens at a
    # word boundary so a runaway response degrades to a shorter complete phrase
    # instead of a fragment cut mid-word.
    title = clip_at_word_boundary(title, 20)
    subtitle = clip_at_word_boundary(subtitle, 40)
    return {"title": title, "subtitle": subtitle}

def trim_scenes(scenes):
    total = sum(korean_char_count(s["text"]) for s in scenes)
    print(f"\n생성된 글자수: {total}자 (목표: {total_chars}자)")
    if total > total_chars * 1.10:
        print("생성 분량이 목표보다 길지만 장면을 유지하고 작업을 계속 진행합니다.")
    else:
        print(f"트리밍 불필요, {len(scenes)}개 장면")
    print(f"문맥에 따라 생성된 장면 수: {len(scenes)}개")
    return scenes


def script_text(result):
    scenes = result.get("scenes") or []
    scene_text = "\n".join(str(scene.get("text", "")) for scene in scenes if isinstance(scene, dict))
    fields = [result.get("title", ""), result.get("description", ""), result.get("final_answer", ""), result.get("evidence_limit", "")]
    return "\n".join(str(item) for item in fields if item) + "\n" + scene_text


def mentioned_comparison_targets(text, targets):
    haystack = re.sub(r"\s+", "", text or "").lower()
    mentioned = []
    for target in targets:
        needle = re.sub(r"\s+", "", str(target or "")).lower()
        if needle and needle in haystack:
            mentioned.append(target)
    return mentioned


def is_generic_final_answer(answer):
    compact = re.sub(r"\s+", "", str(answer or ""))
    if any(re.sub(r"\s+", "", pattern) in compact for pattern in GENERIC_FINAL_ANSWERS):
        return True
    return "따라" in compact and any(token in compact for token in ("다르", "다릅", "달라"))


def unsupported_winner_claim(result, strategy):
    evidence_status = str(strategy.get("evidence_status") or result.get("evidence_status") or "").lower()
    if evidence_status not in INSUFFICIENT_EVIDENCE_STATUSES:
        return False
    answer = str(result.get("final_answer") or "")
    decisive = any(token in answer for token in ("가 더 좋", "이 더 좋", "더 낫", "더 추천", "선택하세요", "승리", "정답은"))
    cautious = any(token in answer for token in ("근거", "부족", "단정", "상황", "개인", "따라", "확인", "상담"))
    return decisive and not cautious


def normalize_story_scenes(result, strategy):
    """Return a copy of `result` with `role` filled in on every scene.

    Stage 2 is asked for it, but a model that drops it must not produce a
    scenes.json with a different shape from every other job's. Roles come
    from the genre's sequence, stretched over however many scenes were
    actually written (the middle roles repeat when there are more scenes than
    roles, which is exactly what the template asks for).

    `visual` and `visual_query` are deliberately absent here — scene_visuals
    plans them after the approval gate, on the text the operator kept.
    """
    if not USE_STORY_TYPES:
        return result
    result = dict(result or {})
    scenes = result.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        return result

    story_type = strategy_story_type(strategy) or story_types.load_config()["default_story_type"]
    sequence = story_types.role_sequence(story_type)
    normalized = []
    for index, raw in enumerate(scenes):
        scene = dict(raw) if isinstance(raw, dict) else {"text": str(raw)}
        role = str(scene.get("role") or "").strip()
        if role not in sequence:
            role = positional_role(index, len(scenes), sequence)
        scene["role"] = role
        normalized.append(scene)

    result["scenes"] = normalized
    result["story_type"] = story_type
    return result


def quality_issue(code, message, level="error"):
    return {"code": code, "level": level, "message": message}


def validate_script(result, strategy):
    topic = strategy.get("topic", "")
    strategy = normalize_strategy_contract(strategy, topic)
    result = dict(result or {})
    scenes = result.get("scenes") or []
    text = script_text(result)
    final_answer = str(result.get("final_answer") or "").strip()
    char_count = sum(korean_char_count(str(scene.get("text", ""))) for scene in scenes if isinstance(scene, dict))
    errors = []
    warnings = []
    if not scenes:
        errors.append(quality_issue("missing_scenes", "scenes가 비어 있습니다."))
    if not final_answer:
        errors.append(quality_issue("missing_final_answer", "final_answer가 비어 있습니다."))
    if not str(result.get("hook_open_loop") or "").strip():
        warnings.append(quality_issue("missing_hook_open_loop", "Scene 1을 미해결로 끝냈는지 확인할 오픈루프 문장이 없습니다.", "warning"))
    hook_pattern = hook_types.normalize(result.get("hook_type") or strategy.get("hook_type"))
    open_loop = str(result.get("hook_open_loop") or "").strip()
    scene1 = str(scenes[0].get("text", "")) if scenes and isinstance(scenes[0], dict) else ""
    # ponytail: digit-only heuristic, add a hangul-numeral table if
    # hook_number_not_concrete fires on >30% of 숫자형 jobs.
    if hook_pattern == hook_types.NUMBER and scene1:
        if not re.search(r"\d", scene1) or any(word in scene1 for word in VAGUE_QUANTIFIERS):
            warnings.append(quality_issue(
                "hook_number_not_concrete",
                "숫자형 훅인데 Scene 1에 구체적 실수치가 없거나 뭉뚱그린 수량 표현이 있습니다.",
                "warning",
            ))
    if open_loop and any(anchor in open_loop for anchor in VAGUE_HOOK_ANCHORS):
        warnings.append(quality_issue(
            "hook_open_loop_not_anchored",
            "오픈루프가 지시대명사('이 방법' 등)에 걸려 있습니다. 구체 명사로 지목하세요.",
            "warning",
        ))
    if "#" in str(result.get("title") or ""):
        warnings.append(quality_issue("hooky_title_hashtag", "제목에 해시태그가 들어 있습니다.", "warning"))
    if result.get("promise_fulfilled") is not True:
        warnings.append(quality_issue("promise_not_fulfilled", "제목과 질문의 약속 충족 여부를 확인해 주세요.", "warning"))
    if USE_STORY_TYPES and scenes:
        roles = [str(scene.get("role") or "") for scene in scenes if isinstance(scene, dict)]
        expected = story_types.role_sequence(strategy_story_type(strategy))
        off_sequence = [role for role in roles if role not in expected]
        if off_sequence:
            warnings.append(quality_issue(
                "role_off_sequence",
                "story_type의 role 시퀀스에 없는 role이 있습니다: " + ", ".join(sorted(set(off_sequence))),
                "warning",
            ))
        if roles and (roles[0] != expected[0] or roles[-1] != expected[-1]):
            warnings.append(quality_issue(
                "role_bookends_missing",
                f"첫 장면은 {expected[0]}, 마지막 장면은 {expected[-1]}이어야 합니다.",
                "warning",
            ))
    if scenes and char_count < total_chars * 0.55:
        warnings.append(quality_issue("under_target_length", f"대본이 목표보다 많이 짧습니다: {char_count}자", "warning"))
    if char_count > total_chars * MAX_SCRIPT_LENGTH_RATIO:
        warnings.append(quality_issue("over_target_length", f"대본이 허용 상한을 넘었습니다: {char_count}자", "warning"))
    if strategy.get("intent_type") == CONTENT_INTENT_COMPARISON:
        targets = strategy.get("comparison_targets") or []
        if len(targets) < 2:
            warnings.append(quality_issue("comparison_requires_two_targets", "비교형 콘텐츠인데 비교 대상이 2개 미만입니다.", "warning"))
        else:
            mentioned = mentioned_comparison_targets(text, targets)
            missing = [target for target in targets if target not in mentioned]
            if missing:
                warnings.append(quality_issue("missing_comparison_targets", "본문이나 최종 답에서 비교 대상이 누락되었습니다: " + ", ".join(missing), "warning"))
        if final_answer and is_generic_final_answer(final_answer):
            warnings.append(quality_issue("generic_final_answer", "비교형 최종 답이 다소 일반적입니다.", "warning"))
        if unsupported_winner_claim(result, strategy):
            warnings.append(quality_issue("unsupported_winner_claim", "근거가 부족한데 한쪽을 승자로 단정했습니다.", "warning"))
    length_ratio = round(char_count / total_chars, 3) if total_chars else None
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "scene_count": len(scenes),
            "korean_char_count": char_count,
            "target_char_count": total_chars,
            "length_ratio": length_ratio,
            "intent_type": strategy.get("intent_type"),
            "comparison_targets": strategy.get("comparison_targets", []),
            "final_answer_present": bool(final_answer),
            # The field a future performance aggregation will GROUP BY. Recording
            # it now is what makes the sample start accumulating.
            "hook_pattern": hook_pattern,
            "hook_open_loop_present": bool(open_loop),
            "story_type": strategy_story_type(strategy) if USE_STORY_TYPES else None,
            # scenes_with_visual_brief is added later, by scene_visuals.
        },
    }


def write_quality_report(report, work_dir=None):
    work_dir = work_dir or WORK_DIR
    os.makedirs(work_dir, exist_ok=True)
    with open(os.path.join(work_dir, "script_quality.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def enforce_quality_without_revision(result, strategy):
    report = validate_script(result, strategy)
    write_quality_report(report)
    if report["warnings"]:
        print("⚠️  대본 품질 참고 사항이 있지만 작업을 계속 진행합니다.")
        for issue in report["warnings"]:
            print(f"  - {issue['code']}: {issue['message']}")
    if report["ok"]:
        return result
    print("⚠️  대본 품질 검증 실패. Claude를 재호출하지 않고 출력 작성을 중단합니다.")
    for issue in report["errors"]:
        print(f"  - {issue['code']}: {issue['message']}")
    raise RuntimeError("대본 품질 검증 실패. 추가 Claude 호출 없이 출력 파일을 작성하지 않습니다.")


def write_outputs(result, strategy, trend_context=None):
    scenes = trim_scenes(result["scenes"])
    # Bookkeeping from normalize_story_scenes; belongs in script_quality.json,
    # not in the scene file every downstream stage reads.
    full_text = "\n\n".join(s["text"] for s in scenes)
    frame_header = normalize_frame_header(result, strategy)
    description = result.get("description", "")

    with open(os.path.join(WORK_DIR, "script.txt"), "w", encoding="utf-8") as f:
        f.write(full_text)
    with open(os.path.join(WORK_DIR, "scenes.json"), "w", encoding="utf-8") as f:
        json.dump(scenes, f, ensure_ascii=False, indent=2)

    meta = {
        "topic":               strategy.get("topic", ""),
        "main_keyword":        strategy.get("main_keyword", ""),
        "search_title_format": strategy.get("search_title_format", ""),
        "search_intent":       strategy.get("search_intent", ""),
        "core_message":        strategy.get("core_message", ""),
        "title":               re.sub(r"\s*#\S+", "", str(result.get("title", strategy.get("title", "")))).strip(),
        "hook_type":           result.get("hook_type", strategy.get("hook_type", "")),
        "hashtags":            result.get("hashtags", ""),
        "frame_header":        frame_header,
        "description":         description,
        "final_answer":        result.get("final_answer", ""),
        "hook_open_loop":      result.get("hook_open_loop", ""),
        "promise_fulfilled":   result.get("promise_fulfilled"),
        "evidence_limit":      result.get("evidence_limit", ""),
        "source_mix":          strategy.get("source_mix", {}),
    }
    if USE_STORY_TYPES:
        meta["story_type"] = strategy_story_type(strategy)
        meta["format_type"] = strategy.get("format_type", "")
    if strategy.get("objective"):
        meta["objective"] = strategy["objective"]
    if strategy.get("content_design"):
        meta["content_design"] = strategy["content_design"]
    if strategy.get("strategy_source"):
        meta["strategy_source"] = strategy["strategy_source"]
    if trend_context:
        meta["trend_context"] = trend_context
    with open(os.path.join(WORK_DIR, "video_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    with open(os.path.join(WORK_DIR, "frame_header.json"), "w", encoding="utf-8") as f:
        json.dump(frame_header, f, ensure_ascii=False, indent=2)

    try:
        content_package.build_content_package(WORK_DIR)
    except Exception as exc:
        # content_package.json feeds downstream cross-platform adapters only —
        # never let it take down a script generation that otherwise succeeded.
        print(f"⚠️  content_package.json 생성 실패(무시하고 계속): {type(exc).__name__}: {exc}")

    print("\n=== 생성된 대본 (TTS용) ==="); print(full_text)
    print(f"\n제목      : {meta['title']}")
    print(f"훅 유형   : {meta['hook_type']}")
    print(f"검색 공식 : {meta['search_title_format']}")
    print(f"핵심 메시지: {meta['core_message']}")
    print(f"프레임 헤더: {frame_header['title']} / {frame_header['subtitle']}")
    print(f"해시태그  : {meta['hashtags']}")
    if USE_STORY_TYPES:
        print(f"스토리 타입: {meta.get('story_type')} / {meta.get('format_type')}")
    # Search queries are not known yet -- scene_visuals prints them once the
    # approved script has been planned.
    print("\n=== 장면 구성 ===")
    for i, s in enumerate(scenes):
        role = f"[{s['role']}] " if s.get("role") else ""
        print(f"{i}: {role}{clip_at_word_boundary(s.get('text'), 40)}")


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────

def main():
    args = parse_args()

    if args.trend:
        collect_trend_candidates(args.trend)
        return

    # ── 주제 결정
    trend_context = None
    pre_strategy = None
    if args.trend_choice:
        topic, trend_context = load_trend_choice(args.trend_choice)
    elif args.topic_json:
        with open(args.topic_json, "r", encoding="utf-8") as f:
            pre_strategy = json.load(f)
        topic = pre_strategy.get("topic") or pre_strategy.get("main_keyword", "")
        print(f"📂 topic JSON 로드: {args.topic_json}")
    else:
        topic = " ".join(args.topic).strip()
        pre_strategy = None
        if not topic:
            print("오류: TOPIC을 입력하세요.")
            print("사용법: python 0_script.py \"주제\"")
            print("       python 0_script.py --topic-json topic.json")
            print("       python 0_script.py --trend \"키워드\"")
            sys.exit(1)

    print(f"주제: {topic}")
    print(
        f"목표: {total_chars}자 / 허용 상한: {int(total_chars * MAX_SCRIPT_LENGTH_RATIO)}자, "
        f"pace={SPEECH_PACE}, density={SCRIPT_DENSITY:.2f}, 장면 수는 문맥에 따라 유연하게 구성"
    )

    # ── 0. YouTube 실데이터 동기화·채널 상대분포 분석
    youtube_guidance = load_youtube_guidance(topic)
    youtube_prompt_text = youtube_guidance.get("prompt_text", "")
    if youtube_guidance.get("available"):
        assessment = youtube_guidance.get("topic_assessment", {})
        print(
            f"📊 YouTube 실데이터 반영: {youtube_guidance.get('strictness_label')} / "
            f"{assessment.get('verdict', '검토')} / sync={youtube_guidance.get('sync_status')}"
        )
    else:
        print("ℹ️  YouTube 성과 표본 없음 (실데이터 없이 계속)")

    # ── 1. PubMed 초록 수집
    try:
        abstracts = fetch_pubmed_abstracts(topic)
    except PubMedSearchError as exc:
        if not args.allow_no_pubmed:
            print(f"PubMed 오류: {exc}"); raise
        abstracts = ("PubMed에서 초록을 찾지 못했습니다. "
                     "과학적 단정은 피하고 일반 설명 중심으로 작성하세요.")
        write_pubmed_status(topic, [], "continued_without_results", str(exc))

    pubmed_query = topic
    pubmed_status = None
    if os.path.exists(PUBMED_STATUS_PATH):
        try:
            with open(PUBMED_STATUS_PATH, "r", encoding="utf-8") as f:
                pubmed_status = json.load(f)
                pubmed_query = pubmed_status.get("pubmed_query") or topic
        except Exception:
            pass

    # ── 2. web_search 보강
    research_policy = adaptive_research_policy(pubmed_status, pre_strategy)
    web_research = ""
    if ENABLE_WEB_RESEARCH and not args.no_web_research and research_policy["web"]:
        web_research = fetch_web_research(topic, pubmed_query)
    else:
        print(f"ℹ️  web_search 생략 (mode={RESEARCH_MODE}, reason={research_policy['reason']})")

    case_research = ""
    if ENABLE_CASE_RESEARCH and not args.no_web_research and research_policy["case"]:
        case_research = fetch_case_and_stat_research(topic, pubmed_query)
    else:
        print(f"ℹ️  case_research 생략 (mode={RESEARCH_MODE}, format 기반)")

    # ── Stage 1: 전략 수립 (Haiku)
    if args.skip_strategy and os.path.exists(STRATEGY_PATH):
        with open(STRATEGY_PATH, "r", encoding="utf-8") as f:
            strategy = json.load(f)
        print(f"⏭️  Stage 1 건너뜀 (기존 strategy.json 사용): {strategy.get('title')}")
    elif args.topic_json and "main_keyword" in pre_strategy:
        # topic JSON에 전략이 이미 있으면 Stage 1 건너뜀
        strategy = pre_strategy
        strategy.setdefault("topic", topic)
        with open(STRATEGY_PATH, "w", encoding="utf-8") as f:
            json.dump(strategy, f, ensure_ascii=False, indent=2)
        print(f"⏭️  Stage 1 건너뜀 (topic JSON 전략 사용): {strategy.get('title')}")
    else:
        strategy = plan_strategy(
            topic,
            abstracts=abstracts,
            web_research=web_research,
            case_research=case_research,
            trend_context=trend_context,
            youtube_guidance=youtube_prompt_text,
            content_design=(pre_strategy or {}).get("content_design"),
        )
        if pre_strategy:
            strategy = merge_planning_contract(strategy, pre_strategy)

    strategy = normalize_strategy_contract(strategy, topic)

    # ── Stage 2: 대본 생성 (Sonnet)
    prompt = build_prompt(strategy, abstracts, trend_context, web_research, youtube_prompt_text)
    with open(os.path.join(WORK_DIR, "claude_prompt.txt"), "w", encoding="utf-8") as f:
        f.write(prompt)

    response = call_claude(prompt)
    result   = parse_claude_json(response)
    result   = revise_overlong_script(result, strategy)
    result   = normalize_story_scenes(result, strategy)
    result   = enforce_quality_without_revision(result, strategy)
    write_outputs(result, strategy, trend_context)


if __name__ == "__main__":
    main()
