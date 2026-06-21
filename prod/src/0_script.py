import argparse
import json
import os
import re
import sys
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

TREND_CANDIDATES_PATH = os.path.join(WORK_DIR, "trend_candidates.json")
PUBMED_STATUS_PATH = os.path.join(WORK_DIR, "pubmed_status.json")

total_chars = int(TARGET_DURATION_SEC * ATEMPO * CHARS_PER_SEC)
prompt_target_chars = int(total_chars * 1.15)
min_scenes_estimate = max(8, prompt_target_chars // 28)


def parse_args():
    parser = argparse.ArgumentParser(description="Shorts script generator")
    parser.add_argument("topic", nargs="*", help="아이디어 또는 주제 문장")
    parser.add_argument("--trend", help="키워드 후보를 뽑을 씨드 단어")
    parser.add_argument("--trend-choice", type=int, help="trend_candidates.json에서 선택할 후보 번호(1부터 시작)")
    parser.add_argument("--allow-no-pubmed", action="store_true", help="PubMed 결과가 없어도 일반 설명 중심으로 계속 생성")
    return parser.parse_args()


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
        scored.append({
            "keyword": keyword,
            "sources": sorted(source_names),
            "score": score,
        })

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
            print(f"- {source}: {error}")

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


class PubMedSearchError(Exception):
    pass


def assess_pubmed_query(topic):
    compact = re.sub(r"\s+", "", topic)
    word_count = len(topic.split())
    if len(compact) <= 2:
        return "주제가 너무 짧습니다. 예: 단어 하나보다 `오메가3 기억력`, `수면 부족 치매 위험`처럼 범위를 조금 넓혀보세요."
    if len(topic) >= 35 or word_count >= 6:
        return "주제가 너무 구체적일 수 있습니다. PubMed 검색용으로는 핵심 의학 키워드 2~4개 정도가 더 잘 맞습니다."
    if re.search(r"추천|가격|순위|고르는법|브랜드|후기|먹는법", topic):
        return "검색어가 소비자/유튜브형 키워드에 가깝습니다. PubMed에는 `효능`, `위험`, `인지기능`, `혈중 지질`처럼 연구 주제형 표현이 더 잘 맞습니다."
    return "PubMed에서 직접 맞는 초록을 찾지 못했습니다. 표현을 더 넓히거나, 건강/질환/기전 중심 키워드로 바꿔보세요."


def write_pubmed_status(topic, pmids, status, message, abstracts_preview=""):
    payload = {
        "topic": topic,
        "status": status,
        "pmids": pmids,
        "pmid_count": len(pmids),
        "message": message,
        "abstracts_preview": abstracts_preview[:1200],
    }
    with open(PUBMED_STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return payload


def fetch_pubmed_abstracts(topic):
    search = request_json(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
        params={"db": "pubmed", "term": topic, "retmax": 5, "sort": "relevance", "retmode": "json"},
    )
    pmids = search.get("esearchresult", {}).get("idlist", [])

    if not pmids:
        message = assess_pubmed_query(topic)
        write_pubmed_status(topic, pmids, "no_results", message)
        return "PubMed에서 직접 관련 초록을 찾지 못했습니다. 이 경우 논문 수치나 특정 연구 결과를 지어내지 말고, 신뢰 가능한 일반 의학 지식과 건강 커뮤니케이션 원칙을 바탕으로 조심스럽게 작성하세요. 근거가 불확실한 내용은 가능성이 있습니다, 도움될 수 있습니다처럼 표현하세요."

    text = request_text(
        "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
        params={"db": "pubmed", "id": ",".join(pmids), "rettype": "abstract", "retmode": "text"},
    )
    write_pubmed_status(topic, pmids, "ok", "PubMed 초록을 찾았습니다.", text)
    return text


def pace_instruction():
    if ATEMPO >= 1.2:
        return "매우 빠르고 에너지 있는 말투로 씁니다. 짧은 문장, 적은 군더더기, 빠르게 치고 나가는 리듬을 사용하세요."
    if ATEMPO >= 1.1:
        return "조금 빠른 대화체로 씁니다. 문장은 짧게, 설명은 압축해서 친근하게 전달하세요."
    return "따뜻하고 여유 있는 대화체로 씁니다. 자연스러운 쉼표와 호흡을 살리세요."


def build_prompt(topic, abstracts, trend_context=None):
    trend_block = ""
    if trend_context:
        candidates = ", ".join(item["keyword"] for item in trend_context.get("candidates", []))
        trend_block = f"""

트렌드 참고 정보:
- 사용자가 처음 던진 단어: {trend_context.get('seed', '')}
- 선택된 키워드: {trend_context.get('selected', {}).get('keyword', topic)}
- 함께 검토된 후보: {candidates}
이 정보는 제목과 훅의 방향을 잡는 데만 사용하고, 본문은 아래 PubMed 근거와 상식적인 건강 커뮤니케이션 원칙을 우선하세요.
"""

    return f"""아래는 '{topic}'와 관련해 PubMed에서 가져온 초록입니다.

{abstracts}{trend_block}

50대 이상 시청자를 위한 한국어 YouTube Shorts 내레이션 대본을 작성하세요. 목표는 이탈률을 낮추고 끝까지 보게 만드는 것입니다.

길이 조건:
- 한국어 글자 기준 최소 {prompt_target_chars}자 이상 작성하세요.
- 장면은 최소 {min_scenes_estimate}개 이상으로 구성하세요.
- 부족한 것보다 약간 넘치는 편이 낫습니다. 너무 길면 후처리에서 줄입니다.

구성:
1. 훅: "어, 이거 내 얘기인가?" 싶게 만드는 질문이나 의외의 사실
2-3. 원리: 왜 그런 현상이 생기는지 설명. 초록에 숫자, 표본 수, 비율, 시간, 연령대가 있으면 구체적으로 넣기
4-5. 비유와 예시: 일상생활에 빗대어 쉽게 설명
6-7. 의외의 세부 내용: 사람들이 잘 모르는 포인트나 추가 수치
8-9. 공감 상황: "이런 적 있으시죠?"처럼 시청자가 자기 경험으로 받아들이게 하기
10. 행동 제안: 오늘 밤이나 내일 아침 바로 할 수 있는 한 가지 행동. 마지막 장면은 반드시 실천 팁이어야 합니다.

문체와 한국어 표현:
- 전체 대본은 한국어로 작성하세요.
- 영어식 직역을 피하고, 한국어 대화 문맥에 맞게 자연스럽게 바꾸세요.
- 50대 이상이 듣기에 편한 존댓말을 사용하되, 강의처럼 딱딱하지 않게 쓰세요.
- 커뮤니티 글, 댓글, 검색어에서 사람들이 실제로 쓰는 말투처럼 구체적이고 생활감 있게 쓰세요.
- 숫자를 무조건 한글로 바꾸지 마세요. 연구 수치, 연령, 비율, 시간처럼 정확성이 중요한 숫자는 아라비아 숫자를 그대로 써도 됩니다.
- 다만 TTS가 어색하게 읽을 수 있는 표현은 띄어쓰기나 조사만 자연스럽게 다듬으세요. 예: "오메가3은"보다 "오메가3는", "50+는"보다 "50대 이상은", "%"보다 "퍼센트".
- 어르신 시청자가 바로 이해할 수 있게 전문용어는 쉬운 말로 먼저 풀어 쓰고, 꼭 필요한 용어만 괄호나 짧은 설명으로 덧붙이세요. 예: "인지기능"은 "기억하고 판단하는 힘", "혈중 지질"은 "피 속 기름 성분"처럼 설명하세요.
- 문장은 짧고 분명하게 쓰고, 병원 강의처럼 딱딱한 표현보다 가족에게 설명하듯 편안한 존댓말을 사용하세요.
- {pace_instruction()}

내용 조건:
- 초록에서 확인 가능한 구체적 숫자나 통계가 있으면 최소 3개 포함하세요.
- PubMed 초록이 없거나 근거가 부족한 경우 숫자, 표본 수, 논문 결과를 지어내지 마세요.
- PubMed 초록이 없는 경우 Claude가 자체 지식 범위에서 신빙성 높은 일반 의학 정보와 콘텐츠 가치가 있는 생활 맥락을 구성하되, 단정 대신 "가능성이 있습니다", "도움이 될 수 있습니다"처럼 표현하세요.
- 마지막 장면은 반드시 실천 가능한 행동 제안이어야 합니다.

각 장면마다 Pexels 영상 검색용 "visual_query"도 작성하세요. visual_query는 2~4개의 영어 키워드로만 작성하세요.

YouTube 업로드용 메타데이터도 함께 작성하세요.
- "title": 본문 핵심과 맞는 한국어 Shorts 제목. 15~25자 권장. 낚시성 과장은 피하고 클릭하고 싶게 쓰세요.
- "summary": 영상 내용을 2~3문장으로 요약하세요. description 상단에 들어갈 문장입니다.
- "hashtags": 이 영상 주제에 맞는 한국어 해시태그 3~5개. #brain50, #뇌건강처럼 고정 채널 태그만 반복하지 마세요.
- "description": 부모님께 보내는 아들이 영상 보기 전에 짧게 소개하는 느낌의 한국어 설명문. 3~5문장, 따뜻한 존댓말로 쓰세요. 대본을 그대로 반복하지 말고 별도 소개글로 쓰세요.

반드시 아래 JSON 객체만 출력하세요. 설명, 마크다운 코드블록, 주석은 출력하지 마세요.

{{
  "title": "제목 텍스트",
  "summary": "요약 텍스트",
  "hashtags": "#태그1 #태그2 #태그3",
  "description": "설명란 인트로 텍스트",
  "scenes": [
    {{"text": "한국어 장면 텍스트", "visual_query": "english search keywords"}}
  ]
}}
"""


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


def write_outputs(result, topic, trend_context=None):
    scenes = trim_scenes(result["scenes"])
    full_text = "\n\n".join(s["text"] for s in scenes)

    video_title = result["title"]
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
    print(f"\n=== 요약 ===\n{video_summary}")
    print(f"\n=== 해시태그 ===\n{video_hashtags}")
    print(f"\n=== 설명란 인트로 ===\n{video_description}")
    print("\n=== 장면별 영상 검색어 ===")
    for i, scene in enumerate(scenes):
        print(f"{i}: {scene['visual_query']}")


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
        sys.exit(1)

    print(f"실제 목표: {total_chars}자 / 프롬프트 요청 목표(여유분 포함): {prompt_target_chars}자, 최소 {min_scenes_estimate}개 장면")
    print(f"선택 주제: {topic}")

    try:
        abstracts = fetch_pubmed_abstracts(topic)
    except PubMedSearchError as exc:
        if not args.allow_no_pubmed:
            print(f"PubMed 검색 결과 없음: {exc}")
            print(f"상세 로그: {PUBMED_STATUS_PATH}")
            raise
        abstracts = "PubMed에서 직접 관련 초록을 찾지 못했습니다. 과학적 단정은 피하고, 일반적인 설명과 실천 팁 중심으로 작성하세요."
        write_pubmed_status(topic, [], "continued_without_results", str(exc))
    prompt = build_prompt(topic, abstracts, trend_context)

    with open(os.path.join(WORK_DIR, "claude_prompt.txt"), "w", encoding="utf-8") as f:
        f.write(prompt)

    response = call_claude(prompt)
    result = parse_claude_json(response)
    write_outputs(result, topic, trend_context)


if __name__ == "__main__":
    main()
