import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import quote

from script_runtime import load_runtime_settings

SETTINGS = load_runtime_settings()
WORK_DIR = SETTINGS.work_dir
os.makedirs(WORK_DIR, exist_ok=True)

ATEMPO = SETTINGS.atempo
TARGET_DURATION_SEC = SETTINGS.target_duration_sec
CHARS_PER_SEC = SETTINGS.chars_per_sec
TREND_CANDIDATE_COUNT = SETTINGS.trend_candidate_count
REQUEST_TIMEOUT = SETTINGS.request_timeout
CLAUDE_TIMEOUT = SETTINGS.claude_timeout
CLAUDE_HTTP_RETRIES = SETTINGS.claude_http_retries
PUBMED_QUERY_TIMEOUT = SETTINGS.pubmed_query_timeout
PUBMED_RETMAX = SETTINGS.pubmed_retmax
PUBMED_ABSTRACT_CHAR_LIMIT = SETTINGS.pubmed_abstract_char_limit
CLAUDE_MODEL = SETTINGS.claude_model
CLAUDE_SCRIPT_MODEL = SETTINGS.claude_script_model
CLAUDE_QUERY_MODEL = SETTINGS.claude_query_model
CLAUDE_STRATEGY_MODEL = SETTINGS.claude_strategy_model
CLAUDE_STRATEGY_FALLBACK_MODELS = SETTINGS.claude_strategy_fallback_models
MAX_TOKENS = SETTINGS.max_tokens
ENABLE_WEB_RESEARCH = SETTINGS.enable_web_research
WEB_RESEARCH_TIMEOUT = SETTINGS.web_research_timeout
WEB_RESEARCH_MAX_USES = SETTINGS.web_research_max_uses
WEB_RESEARCH_MAX_TOKENS = SETTINGS.web_research_max_tokens
WEB_RESEARCH_MAX_TOOL_TURNS = SETTINGS.web_research_max_tool_turns
STRATEGY_PATH = SETTINGS.strategy_path
INSIGHTS_PATH = SETTINGS.insights_path
total_chars = SETTINGS.total_chars
prompt_target_chars = SETTINGS.prompt_target_chars
min_scenes_estimate = SETTINGS.min_scenes_estimate

TREND_CANDIDATES_PATH = os.path.join(WORK_DIR, "trend_candidates.json")
PUBMED_STATUS_PATH = os.path.join(WORK_DIR, "pubmed_status.json")
CLAUDE_USAGE_PATH = os.path.join(WORK_DIR, "claude_usage.jsonl")
CLAUDE_TRANSIENT_STATUSES = (429, 500, 502, 503, 504)


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


def is_invalid_model_error(response):
    if response.status_code != 400:
        return False
    try:
        body = response.json()
    except Exception:
        body = {"raw": response.text}
    text = json.dumps(body, ensure_ascii=False).lower()
    return "model" in text and ("not found" in text or "invalid" in text or "does not exist" in text)


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


def record_claude_usage(stage, model, response):
    usage = response.get("usage") or {}
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "stage": stage,
        "model": model,
        "response_id": response.get("id"),
        "request_id": response.get("_request_id"),
        "stop_reason": response.get("stop_reason"),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }
    with open(CLAUDE_USAGE_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def call_claude_api(payload, timeout, label, stage):
    import requests

    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    attempts = CLAUDE_HTTP_RETRIES + 1
    last_error = None

    for attempt in range(1, attempts + 1):
        try:
            print(f"{label} 호출 시도 {attempt}/{attempts} (timeout={timeout}s)")
            res = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=timeout,
            )
            if res.status_code in CLAUDE_TRANSIENT_STATUSES:
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
            raise RuntimeError(
                f"{label} 응답 대기 시간이 초과되었습니다. 이미 서버에서 처리 중일 수 있어 "
                "자동 재시도하지 않습니다. 같은 JOB을 바로 재시작하면 중복 과금될 수 있습니다."
            ) from exc
        except (requests.ConnectTimeout, requests.ConnectionError, requests.Timeout) as exc:
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

def assess_pubmed_query(topic):
    compact = re.sub(r"\s+", "", topic)
    if len(compact) <= 2:
        return "주제가 너무 짧습니다."
    if len(topic) >= 35 or len(topic.split()) >= 6:
        return "주제가 너무 구체적입니다. 핵심 키워드 2~4개로 줄여보세요."
    if re.search(r"추천|가격|순위|고르는법|브랜드|후기|먹는법", topic):
        return "소비자형 키워드입니다. 효능/위험/기전 중심으로 바꿔보세요."
    return "PubMed에서 직접 맞는 초록을 찾지 못했습니다."

def write_pubmed_status(topic, pmids, status, message, abstracts_preview="", pubmed_query=None):
    payload = {"topic": topic, "pubmed_query": pubmed_query or topic,
               "status": status, "pmids": pmids, "pmid_count": len(pmids),
               "message": message, "abstracts_preview": abstracts_preview[:1200]}
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

def fetch_pubmed_abstracts(topic):
    pubmed_query = translate_pubmed_query(topic)
    search = request_json(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={"db": "pubmed", "term": pubmed_query, "retmax": PUBMED_RETMAX, "sort": "relevance", "retmode": "json"},
    )
    pmids = search.get("esearchresult", {}).get("idlist", [])
    if not pmids:
        message = assess_pubmed_query(pubmed_query)
        write_pubmed_status(topic, pmids, "no_results", message, pubmed_query=pubmed_query)
        return "PubMed에서 직접 관련 초록을 찾지 못했습니다. 이 경우 논문 수치나 특정 연구 결과를 지어내지 말고, 신뢰 가능한 일반 의학 지식과 건강 커뮤니케이션 원칙을 바탕으로 조심스럽게 작성하세요. 근거가 불확실한 내용은 가능성이 있습니다, 도움될 수 있습니다처럼 표현하세요."

    text = request_text(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params={"db": "pubmed", "id": ",".join(pmids), "rettype": "abstract", "retmode": "text"},
    )
    text = limit_pubmed_abstracts(text)
    write_pubmed_status(topic, pmids, "ok", "PubMed 초록을 찾았습니다.", text, pubmed_query=pubmed_query)
    return text


# ─────────────────────────────────────────────
# Claude 공통 호출 (tool_use 멀티턴 루프)
# ─────────────────────────────────────────────

def _call_claude_loop(messages, tools=None, max_tokens=1500, model=None, timeout=None, max_turns=10):
    import requests
    headers = {"x-api-key": os.environ["ANTHROPIC_API_KEY"],
               "anthropic-version": "2023-06-01", "content-type": "application/json"}
    timeout = timeout or CLAUDE_TIMEOUT
    model   = model or CLAUDE_SCRIPT_MODEL
    current_messages = list(messages)

    for _ in range(max_turns):
        payload = {"model": model, "max_tokens": max_tokens, "messages": current_messages}
        if tools:
            payload["tools"] = tools
        res = requests.post("https://api.anthropic.com/v1/messages",
                            headers=headers, json=payload, timeout=timeout)
        res.raise_for_status()
        data    = res.json()
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
            model=CLAUDE_SCRIPT_MODEL,
            timeout=WEB_RESEARCH_TIMEOUT,
            max_turns=WEB_RESEARCH_MAX_TOOL_TURNS,
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


# ─────────────────────────────────────────────
# 피드백 인사이트 로더
# ─────────────────────────────────────────────

def load_feedback_insights():
    if not os.path.exists(INSIGHTS_PATH):
        return ""
    try:
        with open(INSIGHTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("prompt_text", "")
    except Exception as exc:
        print(f"⚠️  인사이트 파일 읽기 실패: {exc}")
        return ""


# ─────────────────────────────────────────────
# Stage 1 — 전략 수립 (Haiku)
# ─────────────────────────────────────────────

def plan_strategy(topic, trend_context=None):
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

    prompt = f"""주제: {topic}{trend_hint}

이 주제로 50대 이상을 위한 YouTube Shorts 콘텐츠 전략을 수립하세요.

[규칙]
main_keyword : YouTube에서 실제 검색할 핵심 키워드 (공백 포함 12자 이내)
sub_keywords : 연관 검색어 2~3개 (배열)
search_intent: 이 키워드를 검색하는 사람의 상황/걱정 (20자 이내)
hook_type    : 두려움형 / 반전형 / 숫자충격형 / 공감형 중 하나
title        : 영상 본문과 훅을 자연스럽게 대표하는 한국어 제목 (15~28자 권장)
               - 사용자가 입력한 주제문을 그대로 복사하거나 어순만 바꾸지 말 것
               - main_keyword는 가능하면 앞쪽에 넣되, 억지스럽거나 기계적인 제목 금지
               - 실제 영상에서 밝혀지는 긴장/반전/해결 약속이 제목에 드러나야 함
               - 과장·공포 조장 대신 "궁금해서 누르게 되는" 생활형 문장으로 작성
search_title_format: 제목 성격 (질문형/비교형/체크리스트형/생활습관형/반전형/공감형 중 하나)
core_message : 시청자가 이 영상에서 가져갈 딱 한 문장 (30자 이내)
thumbnail_text: 썸네일용 짧은 문구 후보 1~2개 (배열, 각 8~14자, 약간 자극적이되 사실 기반)
frame_header : 상단 프레임용 2줄 훅 후보. 대본 맥락을 압축한 추상적이지만 이해 가능한 문구
               - title 3~7자 권장·최대 9자, subtitle 7~14자 권장·최대 18자
               - title은 subtitle보다 반드시 짧게 잡아 위는 짧고 아래는 긴 삼각형 구도로 만들 것
               - 사용자가 입력한 주제어를 그대로 복사하지 말 것
               - 호기심·반전·해결 약속이 느껴지게 작성
cta_next     : 다음 영상 예고 주제 (파생 주제, 20자 이내)

JSON만 출력. 설명·주석·마크다운 없이.

{{
  "main_keyword": "",
  "sub_keywords": [],
  "search_intent": "",
  "hook_type": "",
  "title": "",
  "search_title_format": "",
  "core_message": "",
  "thumbnail_text": [],
  "frame_header": {{"title": "", "subtitle": ""}},
  "cta_next": ""
}}"""

    headers = {"x-api-key": os.environ["ANTHROPIC_API_KEY"],
               "anthropic-version": "2023-06-01", "content-type": "application/json"}
    payload = {"max_tokens": 600,
               "messages": [{"role": "user", "content": prompt}]}

    last_err = None
    for model in strategy_model_candidates():
        payload["model"] = model
        for attempt in range(1, 4):
            try:
                print(f"📋 Stage 1: 전략 수립 중 (시도 {attempt}, model={model})...")
                res = requests.post("https://api.anthropic.com/v1/messages",
                                    headers=headers, json=payload, timeout=30)
                if is_invalid_model_error(res):
                    last_err = Exception(describe_http_error(res))
                    print(f"  ⚠️  모델 오류, 다음 후보로 전환: {last_err}")
                    break
                if res.status_code >= 400:
                    last_err = Exception(describe_http_error(res))
                    res.raise_for_status()
                raw = res.json()["content"][0]["text"].strip()
                raw = re.sub(r"^```(?:json)?", "", raw).rstrip("`").strip()
                strategy = json.loads(raw)
                strategy["topic"] = topic  # 원본 보존

                print(f"  ✅ main_keyword    : {strategy.get('main_keyword')}")
                print(f"  ✅ title           : {strategy.get('title')}")
                print(f"  ✅ hook_type       : {strategy.get('hook_type')}")
                print(f"  ✅ search_format   : {strategy.get('search_title_format')}")
                print(f"  ✅ core_message    : {strategy.get('core_message')}")
                print(f"  ✅ thumbnail_text  : {strategy.get('thumbnail_text')}")

                with open(STRATEGY_PATH, "w", encoding="utf-8") as f:
                    json.dump(strategy, f, ensure_ascii=False, indent=2)
                return strategy

            except Exception as e:
                last_err = e
                print(f"  ⚠️  실패: {e}")
                time.sleep(3)

    raise RuntimeError(f"Stage 1 전략 수립 실패: {last_err}")


# ─────────────────────────────────────────────
# Stage 2 — 프롬프트 빌더
# ─────────────────────────────────────────────

def pace_instruction():
    if ATEMPO >= 1.2:
        return "매우 빠르고 에너지 있는 말투. 짧은 문장, 빠른 리듬."
    if ATEMPO >= 1.1:
        return "조금 빠른 대화체. 문장은 짧게, 압축해서 전달."
    return "따뜻하고 여유 있는 대화체. 자연스러운 쉼표와 호흡."


def build_prompt(strategy, abstracts, trend_context=None, web_research="", feedback_insights=""):
    main_keyword   = strategy.get("main_keyword", "")
    hook_type      = strategy.get("hook_type", "두려움형")
    title          = strategy.get("title", "")
    core_message   = strategy.get("core_message", "")
    search_intent  = strategy.get("search_intent", "")
    cta_next       = strategy.get("cta_next", "")
    topic          = strategy.get("topic", main_keyword)
    search_format  = strategy.get("search_title_format", "")
    thumbnail_text = strategy.get("thumbnail_text", [])
    if isinstance(thumbnail_text, list):
        thumbnail_hint = " / ".join(str(item) for item in thumbnail_text if item)
    else:
        thumbnail_hint = str(thumbnail_text or "")

    # ── 트렌드 블록
    trend_block = ""
    if trend_context:
        candidates = ", ".join(i["keyword"] for i in trend_context.get("candidates", []))
        trend_block = (f"\n트렌드 참고: 씨드={trend_context.get('seed','')}, "
                       f"선택={trend_context.get('selected',{}).get('keyword', topic)}, "
                       f"후보={candidates}\n")

    # ── web_search 블록
    web_block = ""
    if web_research:
        web_block = f"\n=== 최신 영문 연구 자료 (web_search) ===\n{web_research}\n===\n구체적 수치와 출처가 있는 내용을 우선 활용하세요.\n"

    # ── 피드백 블록
    feedback_block = ""
    if feedback_insights:
        feedback_block = (f"\n{feedback_insights}\n"
                          "※ 샘플 수 3 미만 항목은 불확실합니다. 근거 자료를 항상 우선하세요.\n")

    return f"""아래는 '{topic}'와 관련한 연구 자료와 콘텐츠 전략입니다. 

당신은 50대 이상 시청자들의 일상적 고민을 진심으로 경청하고, 불안감을 따뜻하게 보듬어주는 '다정한 동네 주치의'이자 스토리텔러입니다. 정보를 다그치듯 나열하지 말고, 자녀나 오랜 친구가 조곤조곤 챙겨주듯 다정한 이야기로 풀어내세요.

=== 연구 자료 및 전략 데이터 ===
[PubMed 초록]
{abstracts}
{web_block}{feedback_block}{trend_block}
[콘텐츠 전략 (Stage 1 결과)]
main_keyword       : {main_keyword}
검색 의도          : {search_intent}
제목 후보          : {title}
훅(Hook) 유형      : {hook_type}
핵심 메시지        : {core_message} (이 메시지가 영상 전체의 따뜻한 결론이 되어야 합니다)
===

위 자료를 바탕으로 유튜브 쇼츠 대본을 작성해 주세요. 시청자의 마음이 '불안'에서 시작해 '이해'를 거쳐, 마지막엔 깊은 '안도감과 희망'으로 이어지도록 흐름을 설계해야 합니다.

─── 📖 따뜻한 스토리텔링 흐름 ───
[1단계: 내 마음을 알아주는 공감 (Scene 1~2)]
- 시청자의 일상적인 걱정({search_intent})을 알아주는 문장으로 시작하세요.
- "{main_keyword}"를 첫 문장에 기계적으로 넣기보다, "혹시 요즘 이런 적 있으셨나요?"처럼 자연스럽게 대화를 건네며 도입부(Scene 1~2 이내)에 편안하게 녹여내세요.

[2단계: 다정한 눈높이 설명 (Scene 3~6)]
- 어려운 의학 수치나 연구 결과를 지식 자랑하듯 설명하지 마세요. "이게 우리 몸속에서 어떤 상태냐면요~"처럼 일상적인 비유(예: 오래 켜둔 전구, 가을철 마른 나무 등)를 들어 쉽게 풀어주세요.
- 시청자가 무안하거나 죄책감을 느끼지 않게 하는 것이 핵심입니다. "여러분이 잘못하신 게 아니라, 자연스러운 현상이에요"라는 뉘앙스를 바닥에 깔아주세요.

[3단계: 안도감을 주는 실천과 다짐 (Scene 7~10)]
- 겁을 주거나 위협하며 끝내지 마세요. 오늘 당장, 당장 힘들이지 않고 시작할 수 있는 '구체적이고 작은 행동 팁 1가지'(시간, 양, 횟수 명시)를 선물하듯 제안하세요.
- 중간에 시청자의 일상을 다정하게 묻는 질문을 던져 댓글을 유도하세요. (예: "오늘 아침엔 다들 무얼 드셨나요? 댓글로 소통해 봐요.")
- 마지막에는 핵심 메시지인 "{core_message}"를 건네며 안도감을 주고, "소중한 가족과 친구분들에게도 이 따뜻한 소식을 공유해 주세요"라는 멘트를 넣으세요.
- 다음 주제인 "{cta_next}"를 예고하며 따뜻하게 인사를 건네며 마무리하세요.

─── 📝 작성 및 포맷 규칙 ───
1. 분량 및 씬: 한국어 기준 최소 {prompt_target_chars}자 이상, 씬(Scene)은 최소 {min_scenes_estimate}개 이상 넉넉하게 구성할 것.
2. 문체 및 톤: {pace_instruction()} 철저하게 존댓말을 사용하며, 다정하고 친근한 말투를 유지하세요.
3. 전문용어 순화: 어려운 용어는 한 장면에 1개 이하로 제한하고, 무조건 쉬운 말로 풀어서 쓰세요. 
   - 예: 인지기능 -> '기억하고 판단하는 힘', 염증 반응 -> '몸속 경보가 켜진 상태'
4. TTS 발음 최적화(매우 중요):
   - %, ~, 화살표 등 모든 기호는 한글 문장으로 완벽히 풀어 쓰세요. (예: 30% -> 30퍼센트, 3~5배 -> 3에서 5배)
   - 숫자 뒤의 단위는 공백 없이 붙여 쓰세요. 영어 약어(LDL, DNA 등)는 그대로 유지합니다.
5. visual_query: 50대 이상 시청자가 보았을 때 마음이 편안해지는 따뜻한 일상 장면을 영어 키워드 2~4개로 묘사하세요. (차가운 병원, MRI, 주사기 등 공포감을 주는 이미지 절대 금지)
   - 예: "senior peaceful sleep morning light", "elderly couple walking park sunrise"
6. frame_header: 상단에 들어갈 2줄 훅. title(대제목)은 짧게, subtitle(소제목)은 조금 더 길게 구성하여 안정감 있는 삼각형 구도를 만드세요. 사용자가 준 단어를 그대로 복사하지 말고, '호기심과 해결책'이 느껴지게 지으세요.

반드시 아래 JSON 객체 포맷으로만 출력하세요. 마크다운이나 추가 설명은 절대 넣지 마세요.

{{
  "title": "본문과 훅을 자연스럽게 대표하는 최종 제목",
  "hook_type": "{hook_type}",
  "main_keyword": "{main_keyword}",
  "search_title_format": "{search_format}",
  "summary": "요약 텍스트",
  "hashtags": "#태그1 #태그2 #태그3",
  "thumbnail_text": ["썸네일 문구 1", "썸네일 문구 2"],
  "frame_header": {{"title": "대제목", "subtitle": "소제목"}},
  "description": "설명란 인트로 텍스트\\n\\n썸네일 문구 후보: {thumbnail_hint}",
  "scenes": [
    {{"text": "한국어 장면 텍스트", "visual_query": "english search keywords"}}
  ]
}}"""


# ─────────────────────────────────────────────
# Stage 2 — Claude 호출
# ─────────────────────────────────────────────

def call_claude(prompt):
    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    return call_claude_api(payload, CLAUDE_TIMEOUT, "Claude 대본 생성", "script")


def parse_claude_json(response):
    print(f"  stop_reason: {response['stop_reason']}, usage: {response['usage']}")
    raw = response["content"][0]["text"]
    with open(os.path.join(WORK_DIR, "raw_response.txt"), "w", encoding="utf-8") as f:
        f.write(raw)
    if response["stop_reason"] == "max_tokens":
        raise Exception("Claude 출력 잘림. MAX_TOKENS를 높이세요.")
    raw = re.sub(r"^```(?:json)?", "", raw.strip()).rstrip("`").strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print("===== Claude Raw ====="); print(raw); print("======================")
        raise Exception(f"JSON 파싱 실패: {e}")


# ─────────────────────────────────────────────
# 트리밍 & 출력
# ─────────────────────────────────────────────

def korean_char_count(text):
    return len(re.sub(r"[^\uAC00-\uD7A3]", "", text))

def target_scene_count():
    return max(8, min(10, min_scenes_estimate))

def split_scene_sentences(text):
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    return parts if len(parts) > 1 else []

def ensure_scene_count(scenes, min_count):
    """Keep B-roll pacing near 8~10 scenes by splitting long generated scenes."""
    if len(scenes) >= min_count:
        return scenes

    scenes = [dict(scene) for scene in scenes]
    while len(scenes) < min_count:
        candidates = [
            (idx, split_scene_sentences(scene.get("text", "")), korean_char_count(scene.get("text", "")))
            for idx, scene in enumerate(scenes)
        ]
        candidates = [(idx, parts, count) for idx, parts, count in candidates if len(parts) >= 2]
        if not candidates:
            break

        idx, parts, _ = max(candidates, key=lambda item: item[2])
        mid = max(1, len(parts) // 2)
        original = scenes[idx]
        left = {**original, "text": " ".join(parts[:mid])}
        right = {**original, "text": " ".join(parts[mid:])}
        scenes[idx:idx + 1] = [left, right]

    return scenes

def normalize_frame_header(result, strategy, thumbnail_items):
    raw = result.get("frame_header") or strategy.get("frame_header") or {}
    if not isinstance(raw, dict):
        raw = {}
    title = str(raw.get("title") or "").strip()
    subtitle = str(raw.get("subtitle") or "").strip()

    if not title:
        title = (thumbnail_items[0] if thumbnail_items else result.get("title") or strategy.get("title") or "브레인피프티").strip()
    if not subtitle:
        subtitle = "오늘의 뇌건강"

    title = re.sub(r"\s+", " ", title)[:9]
    subtitle = re.sub(r"\s+", " ", subtitle)[:18]
    if len(title) >= len(subtitle) and len(title) > 3:
        title = title[:max(3, min(7, len(subtitle) - 1))]
    return {"title": title, "subtitle": subtitle}

def trim_scenes(scenes):
    total = sum(korean_char_count(s["text"]) for s in scenes)
    print(f"\n생성된 글자수: {total}자 (목표: {total_chars}자)")
    if total > total_chars * 1.10:
        last = scenes[-1]
        body = scenes[:-1]
        running = korean_char_count(last["text"])
        kept = []
        for s in body:
            cnt = korean_char_count(s["text"])
            if running + cnt <= total_chars * 1.05:
                kept.append(s); running += cnt
            else:
                break
        scenes = kept + [last]
        print(f"트리밍 후: {sum(korean_char_count(s['text']) for s in scenes)}자, {len(scenes)}개 장면")
    else:
        print(f"트리밍 불필요, {len(scenes)}개 장면")
    scenes = ensure_scene_count(scenes, target_scene_count())
    print(f"장면 수 보정 후: {len(scenes)}개 장면 (목표: {target_scene_count()}개)")
    return scenes

def write_outputs(result, strategy, trend_context=None):
    scenes = trim_scenes(result["scenes"])
    full_text = "\n\n".join(s["text"] for s in scenes)
    thumbnail_text = result.get("thumbnail_text", strategy.get("thumbnail_text", []))
    if isinstance(thumbnail_text, str):
        thumbnail_items = [thumbnail_text]
    else:
        thumbnail_items = [str(item) for item in thumbnail_text if item]
    frame_header = normalize_frame_header(result, strategy, thumbnail_items)
    description = result.get("description", "")
    if thumbnail_items and "썸네일 문구" not in description:
        description = (
            f"{description.rstrip()}\n\n"
            f"썸네일 문구 후보: {' / '.join(thumbnail_items[:2])}"
        ).strip()

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
        "title":               result.get("title", strategy.get("title", "")),
        "hook_type":           result.get("hook_type", strategy.get("hook_type", "")),
        "summary":             result.get("summary", ""),
        "hashtags":            result.get("hashtags", ""),
        "thumbnail_text":      thumbnail_items,
        "frame_header":        frame_header,
        "description":         description,
    }
    if trend_context:
        meta["trend_context"] = trend_context
    with open(os.path.join(WORK_DIR, "video_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    with open(os.path.join(WORK_DIR, "frame_header.json"), "w", encoding="utf-8") as f:
        json.dump(frame_header, f, ensure_ascii=False, indent=2)

    print("\n=== 생성된 대본 (TTS용) ==="); print(full_text)
    print(f"\n제목      : {meta['title']}")
    print(f"훅 유형   : {meta['hook_type']}")
    print(f"검색 공식 : {meta['search_title_format']}")
    print(f"핵심 메시지: {meta['core_message']}")
    print(f"프레임 헤더: {frame_header['title']} / {frame_header['subtitle']}")
    print(f"해시태그  : {meta['hashtags']}")
    print("\n=== 장면별 영상 검색어 ===")
    for i, s in enumerate(scenes):
        print(f"{i}: {s['visual_query']}")


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
    print(f"목표: {total_chars}자 / 프롬프트 요청: {prompt_target_chars}자, 최소 {min_scenes_estimate}개 장면")

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
    if os.path.exists(PUBMED_STATUS_PATH):
        try:
            with open(PUBMED_STATUS_PATH, "r", encoding="utf-8") as f:
                pubmed_query = json.load(f).get("pubmed_query") or topic
        except Exception:
            pass

    # ── 2. web_search 보강
    web_research = ""
    if ENABLE_WEB_RESEARCH and not args.no_web_research:
        web_research = fetch_web_research(topic, pubmed_query)
    else:
        print("ℹ️  web_search 비활성화")

    # ── 3. 피드백 인사이트 로드
    feedback_insights = load_feedback_insights()
    if feedback_insights:
        print(f"📊 피드백 인사이트 반영")
    else:
        print("ℹ️  피드백 인사이트 없음 (python 5_feedback.py insights 로 생성 가능)")

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
        strategy = plan_strategy(topic, trend_context)

    # ── Stage 2: 대본 생성 (Sonnet)
    prompt = build_prompt(strategy, abstracts, trend_context, web_research, feedback_insights)
    with open(os.path.join(WORK_DIR, "claude_prompt.txt"), "w", encoding="utf-8") as f:
        f.write(prompt)

    response = call_claude(prompt)
    result   = parse_claude_json(response)
    write_outputs(result, strategy, trend_context)


if __name__ == "__main__":
    main()
