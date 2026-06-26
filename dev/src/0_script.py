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

ATEMPO = float(os.environ.get("ATEMPO", "1.0"))
TARGET_DURATION_SEC = int(os.environ.get("TARGET_DURATION_SEC", "60"))
CHARS_PER_SEC = float(os.environ.get("CHARS_PER_SEC", "4.66"))
TREND_CANDIDATE_COUNT = int(os.environ.get("TREND_CANDIDATE_COUNT", "5"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "20"))
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "180"))
CLAUDE_RETRIES = int(os.environ.get("CLAUDE_RETRIES", "2"))
PUBMED_QUERY_TIMEOUT = int(os.environ.get("PUBMED_QUERY_TIMEOUT", "60"))
WEB_RESEARCH_TIMEOUT = int(os.environ.get("WEB_RESEARCH_TIMEOUT", "120"))
ENABLE_WEB_RESEARCH = os.environ.get("ENABLE_WEB_RESEARCH", "true").lower() != "false"

TREND_CANDIDATES_PATH = os.path.join(WORK_DIR, "trend_candidates.json")
PUBMED_STATUS_PATH    = os.path.join(WORK_DIR, "pubmed_status.json")
_DATA_DIR             = os.path.normpath(os.path.join(WORK_DIR, ".."))
INSIGHTS_PATH         = os.environ.get("FEEDBACK_INSIGHTS", os.path.join(_DATA_DIR, "feedback_insights.json"))

total_chars = int(TARGET_DURATION_SEC * ATEMPO * CHARS_PER_SEC)
prompt_target_chars = int(total_chars * 1.15)
min_scenes_estimate = max(8, prompt_target_chars // 28)


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Shorts script generator")
    parser.add_argument("topic", nargs="*", help="아이디어 또는 주제 문장")
    parser.add_argument("--trend", help="키워드 후보를 뽑을 씨드 단어")
    parser.add_argument("--trend-choice", type=int, help="trend_candidates.json에서 선택할 후보 번호(1부터 시작)")
    parser.add_argument("--allow-no-pubmed", action="store_true", help="PubMed 결과가 없어도 일반 설명 중심으로 계속 생성")
    parser.add_argument("--no-web-research", action="store_true", help="web_search 보강 비활성화")
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
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params)}"
        request_headers = {"User-Agent": "Mozilla/5.0"}
        request_headers.update(headers or {})
        request = Request(url, headers=request_headers)
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            text = response.read().decode(charset, errors="replace").strip()
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
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}{urlencode(params)}"
        request_headers = {"User-Agent": "Mozilla/5.0"}
        request_headers.update(headers or {})
        request = Request(url, headers=request_headers)
        with urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")


# ─────────────────────────────────────────────
# 트렌드 후보
# ─────────────────────────────────────────────

def fetch_google_suggestions(seed):
    data = request_json(
        "https://suggestqueries.google.com/complete/search",
        params={"client": "firefox", "hl": "ko", "gl": "KR", "ie": "utf-8", "oe": "utf-8", "q": seed},
    )
    return data[1] if len(data) > 1 else []


def fetch_youtube_suggestions(seed):
    data = request_json(
        "https://suggestqueries.google.com/complete/search",
        params={"client": "firefox", "ds": "yt", "hl": "ko", "gl": "KR", "ie": "utf-8", "oe": "utf-8", "q": seed},
    )
    return data[1] if len(data) > 1 else []


def fetch_google_trends_topics(seed):
    url = f"https://trends.google.com/trends/api/autocomplete/{quote(seed)}"
    data = request_json(url, params={"hl": "ko", "tz": "-540"})
    topics = data.get("default", {}).get("topics", [])
    return [t.get("title") for t in topics if t.get("title")]


def normalize_keyword(text):
    text = re.sub(r"\s+", " ", str(text)).strip()
    return text.strip(" \t\n\r-_/|,.")


def collect_trend_candidates(seed):
    sources = {
        "google_suggest": fetch_google_suggestions,
        "youtube_suggest": fetch_youtube_suggestions,
        "google_trends_topic": fetch_google_trends_topics,
    }
    grouped = defaultdict(set)
    errors = {}
    for source, fetcher in sources.items():
        try:
            for keyword in fetcher(seed):
                normalized = normalize_keyword(keyword)
                if normalized and len(normalized) <= 40:
                    grouped[normalized].add(source)
        except Exception as exc:
            errors[source] = str(exc)

    scored = []
    for keyword, source_names in grouped.items():
        score = len(source_names) * 10
        if seed.replace(" ", "") in keyword.replace(" ", ""):
            score += 3
        if 4 <= len(keyword) <= 20:
            score += 2
        scored.append({"keyword": keyword, "sources": sorted(source_names), "score": score})
    scored.sort(key=lambda item: (-item["score"], item["keyword"]))
    candidates = scored[:TREND_CANDIDATE_COUNT]

    payload = {"seed": seed, "candidates": candidates, "errors": errors}
    with open(TREND_CANDIDATES_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"트렌드 후보 저장: {TREND_CANDIDATES_PATH}")
    for i, item in enumerate(candidates, start=1):
        print(f"{i}. {item['keyword']} ({', '.join(item['sources'])})")
    if errors:
        print("일부 트렌드 소스 조회 실패:")
        for source, error in errors.items():
            print(f"  - {source}: {error}")
    if not candidates:
        raise Exception("트렌드 후보를 찾지 못했습니다. 다른 키워드로 다시 시도하세요.")


def load_trend_choice(choice):
    if not os.path.exists(TREND_CANDIDATES_PATH):
        raise Exception("trend_candidates.json이 없습니다. 먼저 --trend 옵션을 실행하세요.")
    with open(TREND_CANDIDATES_PATH, "r", encoding="utf-8") as f:
        payload = json.load(f)
    candidates = payload.get("candidates", [])
    index = choice - 1
    if index < 0 or index >= len(candidates):
        raise Exception(f"선택 번호가 범위를 벗어났습니다: {choice}")
    selected = candidates[index]
    return selected["keyword"], {
        "seed": payload.get("seed", ""),
        "selected": selected,
        "candidates": candidates,
    }


# ─────────────────────────────────────────────
# PubMed
# ─────────────────────────────────────────────

class PubMedSearchError(Exception):
    pass


def assess_pubmed_query(topic):
    compact = re.sub(r"\s+", "", topic)
    word_count = len(topic.split())
    if len(compact) <= 2:
        return "주제가 너무 짧습니다. 예: `오메가3 기억력`, `수면 부족 치매 위험`처럼 범위를 조금 넓혀보세요."
    if len(topic) >= 35 or word_count >= 6:
        return "주제가 너무 구체적일 수 있습니다. PubMed 검색용으로는 핵심 의학 키워드 2~4개 정도가 더 잘 맞습니다."
    if re.search(r"추천|가격|순위|고르는법|브랜드|후기|먹는법", topic):
        return "검색어가 소비자/유튜브형 키워드에 가깝습니다. PubMed에는 `효능`, `위험`, `인지기능`, `혈중 지질`처럼 연구 주제형 표현이 더 잘 맞습니다."
    return "PubMed에서 직접 맞는 초록을 찾지 못했습니다. 표현을 더 넓히거나, 건강/질환/기전 중심 키워드로 바꿔보세요."


def write_pubmed_status(topic, pmids, status, message, abstracts_preview="", pubmed_query=None):
    payload = {
        "topic": topic,
        "pubmed_query": pubmed_query or topic,
        "status": status,
        "pmids": pmids,
        "pmid_count": len(pmids),
        "message": message,
        "abstracts_preview": abstracts_preview[:1200],
    }
    with open(PUBMED_STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


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
        "model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
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
    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    last_error = None
    for attempt in range(1, CLAUDE_RETRIES + 2):
        try:
            print(f"PubMed 검색어 영어 변환 시도 {attempt}/{CLAUDE_RETRIES + 1} (timeout={PUBMED_QUERY_TIMEOUT}s)")
            res = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=PUBMED_QUERY_TIMEOUT,
            )
            if res.status_code in (429, 500, 502, 503, 504):
                last_error = requests.HTTPError(f"Claude transient status {res.status_code}: {res.text[:500]}")
                if attempt <= CLAUDE_RETRIES:
                    time.sleep(min(5 * attempt, 15))
                continue
            res.raise_for_status()
            translated = clean_pubmed_query(res.json()["content"][0]["text"])
            if translated and not contains_korean(translated):
                print(f"PubMed 검색어: {topic} -> {translated}")
                return translated
            last_error = RuntimeError(f"invalid translated query: {translated}")
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            print(f"PubMed 검색어 변환 실패: {type(exc).__name__}: {exc}")
        except requests.HTTPError as exc:
            last_error = exc
            status = exc.response.status_code if exc.response is not None else None
            if status not in (429, 500, 502, 503, 504):
                raise
            if attempt <= CLAUDE_RETRIES:
                time.sleep(min(5 * attempt, 15))
    print(f"PubMed 검색어 영어 변환 실패. 원문으로 검색합니다: {last_error}")
    return topic


def fetch_pubmed_abstracts(topic):
    pubmed_query = translate_pubmed_query(topic)
    search = request_json(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={"db": "pubmed", "term": pubmed_query, "retmax": 5, "sort": "relevance", "retmode": "json"},
    )
    pmids = search.get("esearchresult", {}).get("idlist", [])
    if not pmids:
        message = assess_pubmed_query(pubmed_query)
        write_pubmed_status(topic, pmids, "no_results", message, pubmed_query=pubmed_query)
        return (
            "PubMed에서 직접 관련 초록을 찾지 못했습니다. "
            "이 경우 논문 수치나 특정 연구 결과를 지어내지 말고, "
            "신뢰 가능한 일반 의학 지식과 건강 커뮤니케이션 원칙을 바탕으로 조심스럽게 작성하세요. "
            "근거가 불확실한 내용은 '가능성이 있습니다', '도움될 수 있습니다'처럼 표현하세요."
        )
    text = request_text(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params={"db": "pubmed", "id": ",".join(pmids), "rettype": "abstract", "retmode": "text"},
    )
    write_pubmed_status(topic, pmids, "ok", "PubMed 초록을 찾았습니다.", text, pubmed_query=pubmed_query)
    return text


# ─────────────────────────────────────────────
# Claude 공통 호출 (tool_use 멀티턴 루프)
# ─────────────────────────────────────────────

def _call_claude_loop(messages, tools=None, max_tokens=1500, timeout=None):
    """
    tool_use stop_reason을 자동으로 처리하는 멀티턴 루프.
    web_search_20250305 등 Anthropic 내장 툴에서는 tool_use 블록의
    content 필드에 검색 결과가 담겨 오므로, 그대로 tool_result로 되돌려 준다.
    """
    import requests

    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    timeout = timeout or CLAUDE_TIMEOUT
    current_messages = list(messages)

    for round_num in range(10):
        payload = {
            "model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
            "max_tokens": max_tokens,
            "messages": current_messages,
        }
        if tools:
            payload["tools"] = tools

        res = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json=payload,
            timeout=timeout,
        )
        res.raise_for_status()
        data = res.json()

        content = data.get("content", [])
        stop_reason = data.get("stop_reason", "end_turn")

        # assistant 턴 누적
        current_messages.append({"role": "assistant", "content": content})

        if stop_reason != "tool_use":
            return data

        # tool_use 블록 처리: 각 도구 결과를 tool_result로 되돌림
        tool_results = []
        for block in content:
            if block.get("type") != "tool_use":
                continue
            # web_search: 검색 결과가 block["content"]에 담겨 있음
            raw_result = block.get("content", "")
            if isinstance(raw_result, list):
                result_str = json.dumps(raw_result, ensure_ascii=False)
            elif isinstance(raw_result, str):
                result_str = raw_result
            else:
                result_str = str(raw_result)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block["id"],
                "content": result_str or "No results returned.",
            })

        if tool_results:
            current_messages.append({"role": "user", "content": tool_results})
        else:
            # tool_use 블록이 있는데 처리할 게 없으면 루프 중단
            break

    return data


# ─────────────────────────────────────────────
# web_search 보강 (최신 영문 연구 자료)
# ─────────────────────────────────────────────

def fetch_web_research(topic, pubmed_query):
    """
    Claude web_search 툴로 최신 영문 뇌과학 연구 자료를 수집한다.
    PubMed 번역에 사용한 영어 쿼리(pubmed_query)를 그대로 재활용하여
    검색 일관성을 유지하고, 우선 출처(Nature Neuroscience, BrainFacts 등)를
    명시적으로 지정한다.
    """
    print(f"🔍 web_search 최신 영문 연구 자료 수집 중... (query: {pubmed_query})")

    messages = [{
        "role": "user",
        "content": (
            f"Search for recent (2022-2026) research and expert findings about: {pubmed_query}\n\n"
            "Prioritize these sources in order:\n"
            "  Academic journals: Nature Neuroscience, Neuron (Cell Press), "
            "Journal of Neuroscience (SfN), PNAS\n"
            "  Science news & outreach: BrainFacts.org (SfN), Neuroscience News, "
            "Scientific American, The Transmitter (Simons Foundation)\n"
            "  Institutions: NIH/NINDS, Harvard Picower Institute, "
            "MIT Brain & Cognitive Sciences, UCSF, Stanford, UCL\n\n"
            "Goal: find 2-3 findings that are useful for a Korean health-education short video "
            "targeting adults aged 50+.\n"
            "Focus on: specific statistics, percentages, sample sizes, timeframes, "
            "risk factors, and actionable lifestyle insights.\n\n"
            "Output: 4-6 bullet points in English. "
            "Each bullet must include the source name, publication year if available, "
            "and a concrete number or finding. No markdown headers."
        ),
    }]

    try:
        data = _call_claude_loop(
            messages,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            max_tokens=1500,
            timeout=WEB_RESEARCH_TIMEOUT,
        )
        result_text = "".join(
            block["text"] for block in data.get("content", []) if block.get("type") == "text"
        ).strip()

        if result_text:
            print(f"✅ web_search 완료 ({len(result_text)}자)")
        else:
            print("⚠️  web_search 응답에서 텍스트를 추출하지 못했습니다.")
        return result_text

    except Exception as exc:
        print(f"⚠️  web_search 실패 (PubMed만으로 계속 진행): {exc}")
        return ""


# ─────────────────────────────────────────────
# 피드백 인사이트 로더
# ─────────────────────────────────────────────

def load_feedback_insights():
    """
    5_feedback.py가 생성한 feedback_insights.json을 읽어
    build_prompt()에 주입할 텍스트를 반환한다.
    파일이 없거나 읽기 실패 시 빈 문자열 반환 (조용히 무시).
    """
    if not os.path.exists(INSIGHTS_PATH):
        return ""
    try:
        with open(INSIGHTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("prompt_text", "")
    except Exception as exc:
        print(f"⚠️  인사이트 파일 읽기 실패 (계속 진행): {exc}")
        return ""


# ─────────────────────────────────────────────
# 프롬프트 빌더
# ─────────────────────────────────────────────

def pace_instruction():
    if ATEMPO >= 1.2:
        return "매우 빠르고 에너지 있는 말투로 씁니다. 짧은 문장, 적은 군더더기, 빠르게 치고 나가는 리듬을 사용하세요."
    if ATEMPO >= 1.1:
        return "조금 빠른 대화체로 씁니다. 문장은 짧게, 설명은 압축해서 친근하게 전달하세요."
    return "따뜻하고 여유 있는 대화체로 씁니다. 자연스러운 쉼표와 호흡을 살리세요."


def build_prompt(topic, abstracts, trend_context=None, web_research="", feedback_insights=""):
    # ── 트렌드 블록
    trend_block = ""
    if trend_context:
        candidates = ", ".join(item["keyword"] for item in trend_context.get("candidates", []))
        trend_block = f"""
트렌드 참고 정보:
- 사용자가 처음 던진 단어: {trend_context.get('seed', '')}
- 선택된 키워드: {trend_context.get('selected', {}).get('keyword', topic)}
- 함께 검토된 후보: {candidates}
이 정보는 제목과 훅의 방향을 잡는 데만 사용하고, 본문은 아래 근거 자료를 우선하세요.
"""

    # ── web_search 보강 블록
    web_block = ""
    if web_research:
        web_block = f"""
=== 최신 영문 연구 자료 (web_search 수집) ===
{web_research}
=== 끝 ===
이 자료는 PubMed 초록과 함께 참고하되, 구체적 수치와 출처가 있는 내용을 우선 활용하세요.
논문명·저자 없이 수치만 있는 경우에도 출처 출판사/기관명을 언급해도 됩니다.
"""

    # ── 피드백 인사이트 블록
    feedback_block = ""
    if feedback_insights:
        feedback_block = f"""
{feedback_insights}

이 인사이트는 과거 영상 반응 데이터를 기반으로 합니다.
- 효과 좋은 훅 유형이 있다면 우선 고려하되, 주제가 맞지 않으면 무시하세요.
- 좋은/나쁜 키워드는 참고만 하고 근거 자료의 정확성을 항상 우선하세요.
- 샘플 수가 적으면(3회 미만) 해당 항목은 통계적으로 불확실합니다.
"""

    return f"""아래는 '{topic}'와 관련한 연구 자료입니다.

=== PubMed 초록 ===
{abstracts}

{web_block}{feedback_block}{trend_block}
50대 이상 시청자를 위한 한국어 YouTube Shorts 내레이션 대본을 작성하세요.
목표는 이탈률을 낮추고 끝까지 보게 만드는 것입니다.

─── 길이 조건 ───
- 한국어 글자 기준 최소 {prompt_target_chars}자 이상 작성하세요.
- 장면은 최소 {min_scenes_estimate}개 이상으로 구성하세요.
- 부족한 것보다 약간 넘치는 편이 낫습니다. 너무 길면 후처리에서 줄입니다.

─── 감정 여정 구조 ───
각 장면은 시청자가 느껴야 할 목표 감정을 의식하며 작성하세요.
정보를 나열하는 것이 아니라, 시청자의 감정이 아래 곡선을 따라가도록 설계하세요:
  불안/호기심 → 이해+놀라움 → 납득+안도 → 흥미+몰입 → 자기인식+공감 → 희망+실천의지

[Scene 1] 훅 (목표 감정: 불안 or 호기심)
  아래 4가지 유형 중 주제에 가장 잘 맞는 하나를 선택하세요.
  선택한 유형을 JSON "hook_type" 필드에 반드시 명시하세요.

  - [두려움형]    "이 증상 있으시면 지금 바로 보세요"
                 → 무시하다 후회할 수 있는 신호를 콕 집어 말하기
  - [반전형]      "사실 대부분이 반대로 알고 계세요"
                 → 시청자가 당연하게 여기던 상식을 뒤집기
  - [숫자충격형]  "60대 3명 중 1명이 이미..."
                 → 생각보다 훨씬 가까운 위험을 수치로 제시
  - [공감형]      "요즘 자꾸 까먹으시죠?"
                 → 시청자가 이미 경험 중인 것을 먼저 말해주기

[Scene 2-3] 원리 (목표 감정: 이해 + 놀라움)
  - 왜 그런 현상이 생기는지 설명
  - 초록/자료에 숫자, 표본 수, 비율, 연령대가 있으면 구체적으로 포함
  - Scene 3은 "이 연구 결과가 이렇게까지 나왔어요?" 하는 놀라움 포인트로 마무리

[Scene 4-5] 비유와 예시 (목표 감정: 납득 + 안도)
  - 일상생활에 빗댄 쉬운 설명으로 이해시키기
  - Scene 5에서는 "그러니까 이게 나쁜 게 아니라, 잘만 하면 된다는 거죠" 식의 안도감

[Scene 6-7] 의외 포인트 (목표 감정: 흥미 + 몰입)
  - 사람들이 잘 모르는 세부 내용이나 추가 수치
  - "이건 진짜 몰랐죠?" 하는 흥미 유발 포인트

[Scene 8-9] 공감 장면 (목표 감정: 자기인식 + 공감)
  - "이런 적 있으시죠?", "혹시 이런 상황이세요?" 형태로 시청자가 자기 경험을 투영하게 하기
  - Scene 9은 공감 후 "그래서 이게 중요한 거예요"로 전환

[Scene 10] 행동 제안 (목표 감정: 희망 + 실천의지) — 반드시 두 파트로 작성
  (a) 오늘 바로 할 수 있는 실천 팁 1가지. 시간/횟수/양을 구체적으로 포함.
      "근거 있는 희망"을 전달하세요. "지금 늦지 않았습니다" 뉘앙스.
  (b) 다음 영상 예고: "다음에는 ~도 알려드릴게요" 형식으로 자연스럽게 채널 연결.
      시리즈 느낌을 주되 강요하지 않게 따뜻하게 끝내세요.

─── 문체와 한국어 표현 ───
- 전체 대본은 한국어로 작성하세요.
- 영어식 직역을 피하고, 한국어 대화 문맥에 맞게 자연스럽게 바꾸세요.
- 50대 이상이 듣기에 편한 존댓말을 사용하되, 강의처럼 딱딱하지 않게 쓰세요.
- 커뮤니티 글, 댓글, 검색어에서 사람들이 실제로 쓰는 말투처럼 생활감 있게 쓰세요.
- 숫자를 무조건 한글로 바꾸지 마세요. 연구 수치, 연령, 비율, 시간은 아라비아 숫자 그대로.
- TTS가 어색하게 읽을 수 있는 표현은 자연스럽게 다듬으세요.
  예: "오메가3는", "50대 이상은", "퍼센트".
- 전문용어는 쉬운 말로 먼저 풀고, 꼭 필요한 경우만 괄호로 보충하세요.
  예: "인지기능"→"기억하고 판단하는 힘", "혈중 지질"→"피 속 기름 성분"
- {pace_instruction()}

─── 내용 조건 ───
- 초록/자료에서 확인 가능한 구체적 숫자나 통계가 있으면 최소 3개 포함하세요.
- 근거가 없는 숫자, 표본 수, 논문 결과는 절대 지어내지 마세요.
- 근거가 불확실한 내용은 "가능성이 있습니다", "도움이 될 수 있습니다"처럼 표현하세요.
- Scene 10은 반드시 실천 팁(a) + 다음 영상 예고(b) 두 파트로 끝내세요.

─── visual_query 작성 규칙 ───
각 장면마다 Pexels 영상 검색용 "visual_query"를 작성하세요.
- 2~4개의 영어 키워드로만 작성하세요.
- 따뜻하고 일상적인 생활 장면을 우선하세요.
- 병원, MRI, 해부도, 뇌 단면도, 주사기, 수술실은 절대 쓰지 마세요.
- 50대 이상이 주인공인 장면을 상상하며 쓰세요.
- 방향 예시:
    수면        → "senior peaceful sleep morning light"
    운동        → "elderly couple walking park sunrise"
    식사/영양   → "healthy colorful food bowl wooden table"
    두뇌/기억   → "senior reading book cafe warm light"
    사회 활동   → "older adults laughing together garden"
    스트레스    → "senior woman meditating nature calm"
    일상 루틴   → "morning routine coffee sunrise senior"
- 의학 용어(neuron, brain scan, synapse 등)보다 생활 장면 키워드를 쓰세요.

─── YouTube 메타데이터 ───
- "title": 본문 핵심과 맞는 한국어 Shorts 제목. 15~25자 권장. 낚시성 과장은 피하고 클릭하고 싶게.
- "hook_type": 선택한 훅 유형 (두려움형 / 반전형 / 숫자충격형 / 공감형 중 정확히 하나)
- "summary": 영상 내용을 2~3문장으로 요약. description 상단에 들어갈 문장.
- "hashtags": 이 영상 주제에 맞는 한국어 해시태그 3~5개. #brain50, #뇌건강처럼 고정 채널 태그만 반복하지 마세요.
- "description": 부모님께 영상을 공유하는 자녀가 짧게 소개하는 느낌의 한국어 설명문.
  3~5문장, 따뜻한 존댓말. 대본을 그대로 반복하지 말고 별도 소개글로 쓰세요.

반드시 아래 JSON 객체만 출력하세요. 설명, 마크다운 코드블록, 주석은 출력하지 마세요.

{{
  "title": "제목 텍스트",
  "hook_type": "선택한 훅 유형",
  "summary": "요약 텍스트",
  "hashtags": "#태그1 #태그2 #태그3",
  "description": "설명란 인트로 텍스트",
  "scenes": [
    {{"text": "한국어 장면 텍스트", "visual_query": "english search keywords"}}
  ]
}}
"""


# ─────────────────────────────────────────────
# Claude 스크립트 생성 호출
# ─────────────────────────────────────────────

def call_claude(prompt):
    import requests

    payload = {
        "model": os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6"),
        "max_tokens": int(os.environ.get("MAX_TOKENS", "4000")),
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    last_error = None
    for attempt in range(1, CLAUDE_RETRIES + 2):
        try:
            print(f"Claude 호출 시도 {attempt}/{CLAUDE_RETRIES + 1} (timeout={CLAUDE_TIMEOUT}s)")
            res = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
                timeout=CLAUDE_TIMEOUT,
            )
            if res.status_code in (429, 500, 502, 503, 504):
                last_error = requests.HTTPError(f"Claude transient status {res.status_code}: {res.text[:500]}")
                if attempt <= CLAUDE_RETRIES:
                    time.sleep(min(10 * attempt, 30))
                continue
            res.raise_for_status()
            return res.json()
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_error = exc
            print(f"Claude 호출 실패: {type(exc).__name__}: {exc}")
            if attempt <= CLAUDE_RETRIES:
                time.sleep(min(10 * attempt, 30))
            continue
        except requests.HTTPError as exc:
            last_error = exc
            status = exc.response.status_code if exc.response is not None else None
            if status in (429, 500, 502, 503, 504) and attempt <= CLAUDE_RETRIES:
                print(f"Claude 일시 오류 {status}. 재시도합니다.")
                time.sleep(min(10 * attempt, 30))
                continue
            raise
    raise RuntimeError(
        f"Claude API 호출이 {CLAUDE_RETRIES + 1}회 실패했습니다. "
        f"CLAUDE_TIMEOUT={CLAUDE_TIMEOUT}s. 마지막 오류: {last_error}"
    )


def parse_claude_json(response):
    print("stop_reason:", response["stop_reason"])
    print("usage:", response["usage"])
    raw = response["content"][0]["text"]
    with open(os.path.join(WORK_DIR, "raw_response.txt"), "w", encoding="utf-8") as f:
        f.write(raw)
    if response["stop_reason"] == "max_tokens":
        raise Exception("Claude output truncated. Increase max_tokens.")
    raw = raw.strip()
    if raw.startswith("```json"):
        raw = raw[len("```json"):]
    if raw.endswith("```"):
        raw = raw[:-3]
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        print("===== Claude Raw =====")
        print(raw)
        print("======================")
        raise Exception(f"JSON 파싱 실패: {e}\nraw_response.txt 파일을 확인하세요.")


# ─────────────────────────────────────────────
# 글자수 트리밍
# ─────────────────────────────────────────────

def korean_char_count(text):
    return len(re.sub(r"[^\uAC00-\uD7A3]", "", text))


def trim_scenes(scenes):
    total_actual = sum(korean_char_count(s["text"]) for s in scenes)
    print(f"\n생성된 글자수: {total_actual}자 (실제 목표: {total_chars}자)")
    if total_actual > total_chars * 1.10:
        action_scene = scenes[-1]
        body_scenes = scenes[:-1]
        running_total = korean_char_count(action_scene["text"])
        kept = []
        for scene in body_scenes:
            count = korean_char_count(scene["text"])
            if running_total + count <= total_chars * 1.05:
                kept.append(scene)
                running_total += count
            else:
                break
        scenes = kept + [action_scene]
        total_actual = sum(korean_char_count(s["text"]) for s in scenes)
        print(f"트리밍 후 글자수: {total_actual}자, 장면 {len(scenes)}개")
    else:
        print(f"트리밍 불필요, 장면 {len(scenes)}개")
    return scenes


# ─────────────────────────────────────────────
# 출력 저장
# ─────────────────────────────────────────────

def write_outputs(result, topic, trend_context=None):
    scenes = trim_scenes(result["scenes"])
    full_text = "\n\n".join(s["text"] for s in scenes)

    video_title = result["title"]
    hook_type = result.get("hook_type", "")
    video_summary = result.get("summary", "")
    video_hashtags = result["hashtags"]
    video_description = result["description"]

    with open(os.path.join(WORK_DIR, "script.txt"), "w", encoding="utf-8") as f:
        f.write(full_text)

    with open(os.path.join(WORK_DIR, "scenes.json"), "w", encoding="utf-8") as f:
        json.dump(scenes, f, ensure_ascii=False, indent=2)

    meta = {
        "topic": topic,
        "title": video_title,
        "hook_type": hook_type,
        "summary": video_summary,
        "hashtags": video_hashtags,
        "description": video_description,
    }
    if trend_context:
        meta["trend_context"] = trend_context

    with open(os.path.join(WORK_DIR, "video_meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    print("=== 생성된 대본 (TTS용) ===")
    print(full_text)
    print(f"\n=== 제목 ===\n{video_title}")
    print(f"\n=== 훅 유형 ===\n{hook_type}")
    print(f"\n=== 요약 ===\n{video_summary}")
    print(f"\n=== 해시태그 ===\n{video_hashtags}")
    print(f"\n=== 설명란 인트로 ===\n{video_description}")
    print("\n=== 장면별 영상 검색어 ===")
    for i, scene in enumerate(scenes):
        print(f"{i}: {scene['visual_query']}")


# ─────────────────────────────────────────────
# 메인
# ─────────────────────────────────────────────

def main():
    args = parse_args()

    if args.trend:
        collect_trend_candidates(args.trend)
        return

    trend_context = None
    if args.trend_choice:
        topic, trend_context = load_trend_choice(args.trend_choice)
    else:
        topic = " ".join(args.topic).strip()
        if not topic:
            print("오류: TOPIC을 입력해주세요.")
            print("사용법: python 0_script.py \"주제 문장\"")
            print("트렌드 후보: python 0_script.py --trend \"키워드\"")
            print("후보 선택: python 0_script.py --trend-choice 1")
            print("web_search 비활성화: python 0_script.py \"주제\" --no-web-research")
            sys.exit(1)

    print(f"실제 목표: {total_chars}자 / 프롬프트 요청 목표(여유분 포함): {prompt_target_chars}자, 최소 {min_scenes_estimate}개 장면")
    print(f"선택 주제: {topic}")

    # ── 1. PubMed 초록 수집
    try:
        abstracts = fetch_pubmed_abstracts(topic)
    except PubMedSearchError as exc:
        if not args.allow_no_pubmed:
            print(f"PubMed 검색 결과 없음: {exc}")
            print(f"상세 로그: {PUBMED_STATUS_PATH}")
            raise
        abstracts = (
            "PubMed에서 직접 관련 초록을 찾지 못했습니다. "
            "과학적 단정은 피하고, 일반적인 설명과 실천 팁 중심으로 작성하세요."
        )
        write_pubmed_status(topic, [], "continued_without_results", str(exc))

    # ── PubMed에서 사용한 영어 쿼리 로드 (web_search에도 재활용)
    pubmed_query = topic  # fallback
    if os.path.exists(PUBMED_STATUS_PATH):
        try:
            with open(PUBMED_STATUS_PATH, "r", encoding="utf-8") as f:
                pubmed_status = json.load(f)
            pubmed_query = pubmed_status.get("pubmed_query") or topic
        except Exception:
            pass

    # ── 2. web_search 최신 영문 연구 자료 보강
    web_research = ""
    use_web = ENABLE_WEB_RESEARCH and not args.no_web_research
    if use_web:
        web_research = fetch_web_research(topic, pubmed_query)
    else:
        print("ℹ️  web_search 비활성화 (ENABLE_WEB_RESEARCH=false 또는 --no-web-research)")

    # ── 3. 피드백 인사이트 로드 (5_feedback.py insights 결과)
    feedback_insights = load_feedback_insights()
    if feedback_insights:
        print(f"📊 피드백 인사이트 반영: {INSIGHTS_PATH}")
    else:
        print("ℹ️  피드백 인사이트 없음 (python 5_feedback.py insights 로 생성 가능)")

    # ── 4. 프롬프트 생성 → Claude 호출
    prompt = build_prompt(topic, abstracts, trend_context, web_research, feedback_insights)

    with open(os.path.join(WORK_DIR, "claude_prompt.txt"), "w", encoding="utf-8") as f:
        f.write(prompt)

    response = call_claude(prompt)
    result = parse_claude_json(response)
    write_outputs(result, topic, trend_context)


if __name__ == "__main__":
    main()
