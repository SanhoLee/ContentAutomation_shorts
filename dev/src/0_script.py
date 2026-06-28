import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from urllib.parse import quote

WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
os.makedirs(WORK_DIR, exist_ok=True)

ATEMPO               = float(os.environ.get("ATEMPO", "1.0"))
TARGET_DURATION_SEC  = int(os.environ.get("TARGET_DURATION_SEC", "60"))
CHARS_PER_SEC        = float(os.environ.get("CHARS_PER_SEC", "4.66"))
TREND_CANDIDATE_COUNT = int(os.environ.get("TREND_CANDIDATE_COUNT", "5"))
REQUEST_TIMEOUT      = int(os.environ.get("REQUEST_TIMEOUT", "20"))
CLAUDE_TIMEOUT       = int(os.environ.get("CLAUDE_TIMEOUT", "180"))
CLAUDE_RETRIES       = int(os.environ.get("CLAUDE_RETRIES", "2"))
PUBMED_QUERY_TIMEOUT = int(os.environ.get("PUBMED_QUERY_TIMEOUT", "60"))
WEB_RESEARCH_TIMEOUT = int(os.environ.get("WEB_RESEARCH_TIMEOUT", "120"))
ENABLE_WEB_RESEARCH  = os.environ.get("ENABLE_WEB_RESEARCH", "true").lower() != "false"

# Stage 1 전략 수립용 모델 (빠르고 저렴한 Haiku)
CLAUDE_STRATEGY_MODEL = os.environ.get("CLAUDE_STRATEGY_MODEL", "claude-3-5-haiku-latest")
CLAUDE_STRATEGY_FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get(
        "CLAUDE_STRATEGY_FALLBACK_MODELS",
        "claude-3-5-haiku-20241022"
    ).split(",")
    if m.strip()
]
# Stage 2 대본 작성용 모델 (품질 집중 Sonnet)
CLAUDE_SCRIPT_MODEL   = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")

TREND_CANDIDATES_PATH = os.path.join(WORK_DIR, "trend_candidates.json")
PUBMED_STATUS_PATH    = os.path.join(WORK_DIR, "pubmed_status.json")
STRATEGY_PATH         = os.path.join(WORK_DIR, "strategy.json")
_DATA_DIR             = os.path.normpath(os.path.join(WORK_DIR, ".."))
INSIGHTS_PATH         = os.environ.get("FEEDBACK_INSIGHTS", os.path.join(_DATA_DIR, "feedback_insights.json"))

total_chars        = int(TARGET_DURATION_SEC * ATEMPO * CHARS_PER_SEC)
prompt_target_chars = int(total_chars * 1.30)
min_scenes_estimate = max(16, prompt_target_chars // 28)


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
    import requests
    payload = {
        "model": CLAUDE_STRATEGY_MODEL,
        "max_tokens": 120,
        "messages": [{"role": "user", "content":
            "Convert Korean health/medical topic to concise English PubMed query. "
            "2-6 biomedical keywords, no Korean, no operators, no explanations.\n\n"
            f"Korean topic: {topic}"}],
    }
    headers = {"x-api-key": os.environ["ANTHROPIC_API_KEY"],
               "anthropic-version": "2023-06-01", "content-type": "application/json"}
    last_err = None
    for model in strategy_model_candidates():
        payload["model"] = model
        for attempt in range(1, CLAUDE_RETRIES + 2):
            try:
                print(f"PubMed 쿼리 번역 시도 {attempt} (model={model})")
                res = requests.post("https://api.anthropic.com/v1/messages",
                                    headers=headers, json=payload, timeout=PUBMED_QUERY_TIMEOUT)
                if is_invalid_model_error(res):
                    last_err = Exception(describe_http_error(res))
                    print(f"  ⚠️  모델 오류, 다음 후보로 전환: {last_err}")
                    break
                if res.status_code in (429, 500, 502, 503, 504):
                    last_err = Exception(describe_http_error(res))
                    time.sleep(min(5 * attempt, 15)); continue
                if res.status_code >= 400:
                    last_err = Exception(describe_http_error(res))
                    res.raise_for_status()
                translated = clean_pubmed_query(res.json()["content"][0]["text"])
                if translated and not contains_korean(translated):
                    print(f"  번역: {topic} → {translated}")
                    return translated
            except Exception as e:
                last_err = e
                print(f"  실패: {e}")
                time.sleep(min(5 * attempt, 15))
    print(f"번역 실패. 원문 사용: {last_err}")
    return topic

def fetch_pubmed_abstracts(topic):
    pubmed_query = translate_pubmed_query(topic)
    search = request_json("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                          params={"db": "pubmed", "term": pubmed_query, "retmax": 5,
                                  "sort": "relevance", "retmode": "json"})
    pmids = search.get("esearchresult", {}).get("idlist", [])
    if not pmids:
        write_pubmed_status(topic, [], "no_results", assess_pubmed_query(pubmed_query), pubmed_query=pubmed_query)
        return ("PubMed에서 직접 관련 초록을 찾지 못했습니다. "
                "논문 수치나 특정 연구 결과를 지어내지 말고, "
                "신뢰 가능한 일반 의학 지식 바탕으로 조심스럽게 작성하세요.")
    text = request_text("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                        params={"db": "pubmed", "id": ",".join(pmids), "rettype": "abstract", "retmode": "text"})
    write_pubmed_status(topic, pmids, "ok", "PubMed 초록 수집 완료", text, pubmed_query=pubmed_query)
    return text


# ─────────────────────────────────────────────
# Claude 공통 호출 (tool_use 멀티턴 루프)
# ─────────────────────────────────────────────

def _call_claude_loop(messages, tools=None, max_tokens=1500, model=None, timeout=None):
    import requests
    headers = {"x-api-key": os.environ["ANTHROPIC_API_KEY"],
               "anthropic-version": "2023-06-01", "content-type": "application/json"}
    timeout = timeout or CLAUDE_TIMEOUT
    model   = model or CLAUDE_SCRIPT_MODEL
    current_messages = list(messages)

    for _ in range(10):
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

def fetch_web_research(topic, pubmed_query):
    print(f"🔍 web_search 최신 영문 연구 자료 수집 중... (query: {pubmed_query})")
    messages = [{"role": "user", "content":
        f"Search for recent (2022-2026) research about: {pubmed_query}\n\n"
        "Prioritize: Nature Neuroscience, Neuron, Journal of Neuroscience, PNAS, "
        "BrainFacts.org, Neuroscience News, Scientific American, The Transmitter, "
        "NIH/NINDS, Harvard Picower, MIT Brain & Cognitive Sciences, UCSF, Stanford, UCL\n\n"
        "Find 2-3 findings for a Korean health video targeting adults 50+. "
        "Focus on specific stats, sample sizes, percentages, actionable insights.\n"
        "Output: 4-6 bullet points in English with source name and year."}]
    try:
        data = _call_claude_loop(messages,
                                 tools=[{"type": "web_search_20250305", "name": "web_search"}],
                                 max_tokens=1500, model=CLAUDE_SCRIPT_MODEL,
                                 timeout=WEB_RESEARCH_TIMEOUT)
        result = "".join(b["text"] for b in data.get("content", []) if b.get("type") == "text").strip()
        print(f"✅ web_search 완료 ({len(result)}자)")
        return result
    except Exception as exc:
        print(f"⚠️  web_search 실패 (계속 진행): {exc}")
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
title        : 아래 4가지 검색형 공식 중 하나를 선택해 작성
               - 질문형: "[키워드], 정말 ~일까?"
               - 비교형: "[A]와 [B] 차이"
               - 체크리스트형: "[대상]이 ~할 때 보는 N가지"
               - 생활습관형: "[습관]이 뇌에 미치는 영향"
               ※ 제목 앞 15자 이내에 main_keyword 반드시 포함
search_title_format: 위 4가지 중 선택한 것 (질문형/비교형/체크리스트형/생활습관형)
core_message : 시청자가 이 영상에서 가져갈 딱 한 문장 (30자 이내)
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
    """
    Stage 1에서 확정된 strategy를 받아 대본 작성 프롬프트를 구성한다.
    Claude Sonnet은 감정 여정과 문장 품질에만 집중한다.
    """
    main_keyword   = strategy.get("main_keyword", "")
    hook_type      = strategy.get("hook_type", "두려움형")
    title          = strategy.get("title", "")
    core_message   = strategy.get("core_message", "")
    search_intent  = strategy.get("search_intent", "")
    cta_next       = strategy.get("cta_next", "")
    topic          = strategy.get("topic", main_keyword)
    search_format  = strategy.get("search_title_format", "")

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

=== PubMed 초록 ===
{abstracts}
{web_block}{feedback_block}{trend_block}
=== 확정된 콘텐츠 전략 (Stage 1 결과 — 변경 불가) ===
main_keyword       : {main_keyword}
검색 의도          : {search_intent}
제목               : {title}  ← 이 제목을 그대로 사용하세요
제목 공식          : {search_format}
훅 유형            : {hook_type}
핵심 메시지        : {core_message}  ← 이 한 가지만 전달하면 됩니다
다음 영상 예고     : {cta_next}
===

당신은 위 전략을 실행하는 대본 작가입니다.
전략(제목·훅 유형·핵심 메시지)은 이미 확정됐으니, 감정 여정과 문장 품질에만 집중하세요.

─── 길이 조건 ───
- 한국어 글자 기준 최소 {prompt_target_chars}자 이상.
- 장면 최소 {min_scenes_estimate}개 이상.
- 약간 넘쳐도 됩니다. 후처리에서 트리밍합니다.

─── 검색 최적화 규칙 (필수) ───
① Scene 1 첫 문장에 "{main_keyword}" 반드시 포함.
  나쁜 예: "혹시 이런 경험 있으세요?"
  좋은 예: "{main_keyword}은(는) ~합니다."
② 제목은 위 전략의 제목을 그대로 JSON title 필드에 출력.

─── 감정 여정 구조 ───
정보 나열이 아니라, 시청자의 감정이 아래 곡선을 따르도록 설계하세요.

[Scene 1] 훅 — 목표 감정: {hook_type}에서 오는 불안/호기심
  첫 문장에 "{main_keyword}" 포함 (위 검색 최적화 규칙 ①)

[Scene 2-3] 원리 — 목표 감정: 이해 + 놀라움
  왜 그런 현상인지 설명. 연구 수치가 있으면 구체적으로.
  Scene 3: "이 연구 결과가 이렇게까지?" 하는 놀라움 포인트

[Scene 4-5] 비유·예시 — 목표 감정: 납득 + 안도
  일상 빗댄 쉬운 설명. Scene 5: "그러니까 지금 바꾸면 된다" 안도감

[Scene 6-7] 의외 포인트 — 목표 감정: 흥미 + 몰입
  잘 모르는 세부 사실. "이건 진짜 몰랐죠?" 흥미 유발

[Scene 8-9] 공감 — 목표 감정: 자기인식 + 공감
  "이런 적 있으시죠?" 시청자가 자기 경험을 투영하게.
  Scene 9: 공감 후 "그래서 이게 중요한 거예요" 전환

[Scene 10] 행동 + 예고 — 목표 감정: 희망 + 실천의지
  (a) 오늘 바로 할 수 있는 실천 팁 1가지. 시간/횟수/양 구체적으로.
      핵심 메시지 "{core_message}"를 자연스럽게 담아 마무리.
  (b) "{cta_next}"로 이어지는 다음 영상 예고.
      "다음에는 ~도 알려드릴게요" 형식으로 따뜻하게 끝.

─── 댓글 트리거 (Scene 8 또는 9에 포함) ───
구체적 질문으로 댓글을 유도하세요.
좋은 예: "여러분은 매일 몇 시간 주무세요? 댓글로 알려주세요."
나쁜 예: "어떻게 생각하세요?" (너무 막연함)

─── 공유 유도 (Scene 10 마지막에 한 문장) ───
"부모님께 이 영상 공유해드리세요." 또는 "소중한 분께 알려주세요."
자연스럽게 붙이세요.

─── 문체 ───
- 전체 한국어, 존댓말, 강의체 금지.
- 아라비아 숫자 유지 (연구 수치, 연령).
- 전문용어는 쉬운 말로 먼저 풀기.
- {pace_instruction()}

─── TTS 발음 최적화 규칙 (필수) ───
아래 규칙을 위반하면 TTS 음성이 씹히거나 자막 타이밍이 어긋납니다.

  ① % → "퍼센트"로 풀어 쓸 것
     ❌ 30%가 감소  ✅ 30퍼센트가 감소

  ② 소수점 숫자 → "점"으로 풀어 쓸 것
     ❌ 3.5배 높아  ✅ 3점5배 높아

  ③ ~ 기호 → "정도" 또는 "에서"로 대체
     ❌ ~50대       ✅ 50대 전후
     ❌ 3~5배       ✅ 3에서 5배

  ④ 화살표·기타 기호 완전 제거 또는 말로 대체
     ❌ 기억력이 → 저하  ✅ 기억력이 저하되고

  ⑤ 영어 약어(LDL, HDL, DNA, BMI, MRI 등)는 영어 그대로 유지
     TTS 후처리(1_tts.py)에서 발음 처리됨. 대본에서 변환 금지.
     ❌ 엘디엘 수치  ✅ LDL 수치

  ⑥ 숫자 뒤 단위는 붙여 쓸 것 (공백 없이)
     ❌ 30 퍼센트   ✅ 30퍼센트

─── 내용 규칙 ───
- 근거 있는 수치 최소 3개 포함.
- 근거 없는 수치·논문 결과 지어내기 금지.
- 불확실한 내용: "가능성이 있습니다", "도움이 될 수 있습니다".

─── visual_query 작성 규칙 ───
- 영어 키워드 2~4개.
- 병원·MRI·해부도·주사기·뇌 단면도 금지.
- 50대 이상이 주인공인 생활 장면.
  예) "senior peaceful sleep morning light"
      "elderly couple walking park sunrise"
      "healthy colorful food bowl wooden table"
      "senior reading book cafe warm light"
      "older adults laughing together garden"

반드시 아래 JSON 객체만 출력. 마크다운·설명·주석 없이.

{{
  "title": "{title}",
  "hook_type": "{hook_type}",
  "main_keyword": "{main_keyword}",
  "search_title_format": "{search_format}",
  "summary": "요약 텍스트",
  "hashtags": "#태그1 #태그2 #태그3",
  "description": "설명란 인트로 텍스트",
  "scenes": [
    {{"text": "한국어 장면 텍스트", "visual_query": "english search keywords"}}
  ]
}}
"""


# ─────────────────────────────────────────────
# Stage 2 — Claude 호출
# ─────────────────────────────────────────────

def call_claude(prompt):
    import requests
    payload = {"model": CLAUDE_SCRIPT_MODEL,
               "max_tokens": int(os.environ.get("MAX_TOKENS", "4000")),
               "messages": [{"role": "user", "content": prompt}]}
    headers = {"x-api-key": os.environ["ANTHROPIC_API_KEY"],
               "anthropic-version": "2023-06-01", "content-type": "application/json"}
    last_err = None
    for attempt in range(1, CLAUDE_RETRIES + 2):
        try:
            print(f"✍️  Stage 2: 대본 작성 중 (시도 {attempt}, timeout={CLAUDE_TIMEOUT}s)...")
            res = requests.post("https://api.anthropic.com/v1/messages",
                                headers=headers, json=payload, timeout=CLAUDE_TIMEOUT)
            if res.status_code in (429, 500, 502, 503, 504):
                last_err = Exception(f"status {res.status_code}")
                time.sleep(min(10 * attempt, 30)); continue
            res.raise_for_status()
            return res.json()
        except Exception as e:
            last_err = e
            print(f"  ⚠️  실패: {e}")
            time.sleep(min(10 * attempt, 30))
    raise RuntimeError(f"Stage 2 대본 생성 {CLAUDE_RETRIES + 1}회 실패: {last_err}")


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
    return scenes

def write_outputs(result, strategy, trend_context=None):
    scenes = trim_scenes(result["scenes"])
    full_text = "\n\n".join(s["text"] for s in scenes)

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
        "description":         result.get("description", ""),
    }
    if trend_context:
        meta["trend_context"] = trend_context
    with open(os.path.join(WORK_DIR, "video_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print("\n=== 생성된 대본 (TTS용) ==="); print(full_text)
    print(f"\n제목      : {meta['title']}")
    print(f"훅 유형   : {meta['hook_type']}")
    print(f"검색 공식 : {meta['search_title_format']}")
    print(f"핵심 메시지: {meta['core_message']}")
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
