import requests
import os
import json
import re

WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
os.makedirs(WORK_DIR, exist_ok=True)

TOPIC = "견과류가 그렇게 좋다던데 실제로 그럴까?"  # <- 여기만 매번 바꾸면 됩니다

ATEMPO = float(os.environ.get("ATEMPO", "1.0"))
TARGET_DURATION_SEC = int(os.environ.get("TARGET_DURATION_SEC", "60"))
CHARS_PER_SEC = float(os.environ.get("CHARS_PER_SEC", "4.66"))

total_chars = int(TARGET_DURATION_SEC * ATEMPO * CHARS_PER_SEC)
prompt_target_chars = int(total_chars * 1.15)
min_scenes_estimate = max(8, prompt_target_chars // 28)

print(f"실제 목표: {total_chars}자 / 프롬프트 요청 목표(여유분 포함): {prompt_target_chars}자, 최소 {min_scenes_estimate}개 장면")


# 1. PubMed에서 관련 논문 검색
search = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi", params={
    "db": "pubmed", "term": TOPIC, "retmax": 5, "sort": "relevance", "retmode": "json"
}).json()
pmids = search["esearchresult"]["idlist"]

# 2. 논문 초록 가져오기
abstracts = requests.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi", params={
    "db": "pubmed", "id": ",".join(pmids), "rettype": "abstract", "retmode": "text"
}).text


# 3. Claude로 대본 작성
if ATEMPO >= 1.2:
    pace_instruction = "Write in a very fast-paced, punchy, energetic style: short sentences, minimal filler words, rapid-fire delivery - like an enthusiastic friend talking quickly."
elif ATEMPO >= 1.1:
    pace_instruction = "Write in a brisk, conversational style: shorter sentences, less filler - like a friendly person talking a bit faster than usual."
else:
    pace_instruction = "Write in a relaxed, warm conversational style with natural pauses."


prompt = f"""Here are PubMed abstracts about '{TOPIC}':

{abstracts}

Write a YouTube Shorts narration script (in KOREAN) for adults aged 50+, designed to maximize watch-through (low drop-off).

**LENGTH: Write AT LEAST {prompt_target_chars} Korean characters total (more is fine - err on the side of writing more scenes rather than fewer). Use at least {min_scenes_estimate} scenes. It's better to overshoot than undershoot.**

NARRATIVE ARC (expand with multiple examples/scenes per section as needed to reach the length target):
1. HOOK - A surprising question or fact ("wait, that's me?")
2-3. MECHANISM - WHY this happens, with SPECIFIC numbers from the abstracts (sample sizes, percentages, hours, age groups)
4-5. ANALOGY + EXAMPLE - Compare the science to everyday life, with a concrete example
6-7. SURPRISING DETAIL - One or two more concrete stats or unexpected findings from the abstracts
8-9. RELATABLE SCENARIO - One or two "이런 적 있으시죠?" type situations
10. ACTION - One specific, doable action for tonight/tomorrow morning (this MUST be the final scene)

Each scene is a short paragraph, roughly 25-35 Korean characters (about 6-8 seconds spoken).

CONTENT REQUIREMENTS:
- Include AT LEAST 3 specific numbers/statistics from the abstracts (no vague phrases like "연구에 따르면 좋다고 합니다")
- {pace_instruction}
- Friendly tone, no jargon
- The LAST scene must always be the actionable tip - this is important for the trimming step later

For EACH scene also write "visual_query": 2-4 English keywords for Pexels stock video search matching that scene's content/mood.

Also provide:
- "title": A catchy YouTube Shorts title in Korean, 15-25 characters, can include relevant emojis (🧠 etc), related to the topic
- "hashtags": 3-5 Korean hashtags (with #) specific to THIS video's topic (e.g. #뇌활성화 #기억력개선) - do NOT repeat generic channel hashtags like #brain50 or #뇌건강
- "description": A YouTube description written in KOREAN, in the voice of an adult son writing to his parents (50s-60s) before they watch this video. Use fully polite/formal Korean (경어, 합니다/해요체), but warm and affectionate, like a son gently introducing something he prepared for his mom and dad. 3-5 sentences. Briefly introduce what this video is about and why it's worth watching - do NOT repeat the narration script verbatim, write it as a separate, caring intro message. Example tone (write your own, don't copy): "오늘은 뇌 건강에 관한 좋은 정보를 가져왔어요. 요즘 깜빡 깜빡하신다고 하셨던 것, 사실 작은 습관 하나로도 많이 달라질 수 있대요. 영상 보시고 오늘부터 같이 한번 해보면 좋을 것 같아요 :)"

**Output ONLY a JSON object in this exact format, no explanation, no markdown code blocks:**

{{
  "title": "제목 텍스트",
  "hashtags": "#태그1 #태그2 #태그3",
  "description": "설명란 텍스트",
  "scenes": [
    {{"text": "한국어 장면 텍스트", "visual_query": "english search keywords"}},
    ...
  ]
}}
"""


res = requests.post(
    "https://api.anthropic.com/v1/messages",
    headers={
        "x-api-key": os.environ["ANTHROPIC_API_KEY"],
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    },
    json={
        "model": "claude-sonnet-4-6",
        "max_tokens": 4000,          # 기존 1500 → 4000
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ]
    }
)

response = res.json()

print("stop_reason:", response["stop_reason"])
print("usage:", response["usage"])

raw = response["content"][0]["text"]

# 디버깅용 저장
with open(os.path.join(WORK_DIR, "raw_response.txt"), "w", encoding="utf-8") as f:
    f.write(raw)

# 토큰 부족으로 잘린 경우
if response["stop_reason"] == "max_tokens":
    raise Exception(
        "Claude output truncated. Increase max_tokens."
    )

# 혹시 ```json ... ``` 형태로 반환한 경우 제거
raw = raw.strip()

if raw.startswith("```json"):
    raw = raw[len("```json"):]

if raw.endswith("```"):
    raw = raw[:-3]

raw = raw.strip()

try:
    result = json.loads(raw)

except json.JSONDecodeError as e:

    print("===== Claude Raw =====")
    print(raw)
    print("======================")

    raise Exception(
        f"JSON 파싱 실패: {e}\n"
        f"raw_response.txt 파일을 확인하세요."
    )





scenes = result["scenes"]
video_title = result["title"]
video_hashtags = result["hashtags"]
video_description = result["description"]


def korean_char_count(text):
    return len(re.sub(r'[^\uAC00-\uD7A3]', '', text))


total_actual = sum(korean_char_count(s["text"]) for s in scenes)
print(f"\n생성된 글자수: {total_actual}자 (실제 목표: {total_chars}자)")

# 마지막 장면(액션)은 항상 유지, 그 앞부터 잘라서 목표치 맞추기
if total_actual > total_chars * 1.10:
    action_scene = scenes[-1]
    body_scenes = scenes[:-1]

    running_total = korean_char_count(action_scene["text"])
    kept = []
    for s in body_scenes:
        c = korean_char_count(s["text"])
        if running_total + c <= total_chars * 1.05:
            kept.append(s)
            running_total += c
        else:
            break

    scenes = kept + [action_scene]
    total_actual = sum(korean_char_count(s["text"]) for s in scenes)
    print(f"트리밍 후 글자수: {total_actual}자, 장면 {len(scenes)}개")
else:
    print(f"트리밍 불필요, 장면 {len(scenes)}개")


# TTS용 전체 텍스트 (장면 사이 빈 줄 2개로 구분)
full_text = "\n\n".join(s["text"] for s in scenes)

with open(os.path.join(WORK_DIR, "script.txt"), "w", encoding="utf-8") as f:
    f.write(full_text)

with open(os.path.join(WORK_DIR, "scenes.json"), "w", encoding="utf-8") as f:
    json.dump(scenes, f, ensure_ascii=False, indent=2)

with open(os.path.join(WORK_DIR, "video_meta.json"), "w", encoding="utf-8") as f:
    json.dump({
        "title": video_title,
        "hashtags": video_hashtags,
        "description": video_description
    }, f, ensure_ascii=False, indent=2)

print("=== 생성된 대본 (TTS용) ===")
print(full_text)
print(f"\n=== 제목 ===\n{video_title}")
print(f"\n=== 해시태그 ===\n{video_hashtags}")
print(f"\n=== 설명란 인트로 ===\n{video_description}")
print("\n=== 장면별 영상 검색어 ===")

for i, s in enumerate(scenes):
    print(f"{i}: {s['visual_query']}")