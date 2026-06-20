import os
import random
import requests

WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]
QUERY = "old person sleep"

PER_PAGE = 30

headers = {
    "Authorization": PEXELS_API_KEY
}

# -----------------------------------
# Step 1. 첫 페이지 조회해서 전체 결과 수 확인
# -----------------------------------
url = (
    "https://api.pexels.com/videos/search"
    f"?query={QUERY}"
    "&orientation=portrait"
    f"&per_page={PER_PAGE}"
    "&page=1"
)

res = requests.get(url, headers=headers)
res.raise_for_status()
data = res.json()

total_results = data["total_results"]

if total_results == 0:
    raise Exception(f"No videos found for query: {QUERY}")

total_pages = (total_results + PER_PAGE - 1) // PER_PAGE

print("====================================")
print(f"Query              : {QUERY}")
print(f"Total results       : {total_results}")
print(f"Per page            : {PER_PAGE}")
print(f"Available pages     : 1 ~ {total_pages}")
print("====================================")

# -----------------------------------
# Step 2. 실제 존재하는 페이지 중 랜덤 선택
# -----------------------------------
random_page = random.randint(1, total_pages)

print(f"Random page index   : {random_page}")

url = (
    "https://api.pexels.com/videos/search"
    f"?query={QUERY}"
    "&orientation=portrait"
    f"&per_page={PER_PAGE}"
    f"&page={random_page}"
)

res = requests.get(url, headers=headers)
res.raise_for_status()
page_data = res.json()

videos = page_data["videos"]

# 혹시 마지막 페이지에서 비어있다면 첫 페이지 fallback
if len(videos) == 0:

    print("WARNING: selected page returned no videos.")
    print("Fallback to page 1.")

    random_page = 1

    url = (
        "https://api.pexels.com/videos/search"
        f"?query={QUERY}"
        "&orientation=portrait"
        f"&per_page={PER_PAGE}"
        "&page=1"
    )

    res = requests.get(url, headers=headers)
    res.raise_for_status()
    videos = res.json()["videos"]

# -----------------------------------
# Step 3. 페이지 내 영상 랜덤 선택
# -----------------------------------
video_idx = random.randrange(len(videos))
video = videos[video_idx]

print(f"Video index         : {video_idx}")
print(f"Video ID            : {video['id']}")

# -----------------------------------
# Step 4. 세로 영상 파일 선택
# -----------------------------------
portrait_files = [
    v for v in video["video_files"]
    if v["width"] < v["height"]
]

if len(portrait_files) == 0:
    portrait_files = video["video_files"]

# 적당한 해상도 우선
preferred = [
    v for v in portrait_files
    if 720 <= v["height"] <= 1920
]

if preferred:
    candidates = preferred
else:
    candidates = portrait_files

resolution_idx = random.randrange(len(candidates))
target = candidates[resolution_idx]

print(
    f"Resolution index    : {resolution_idx}"
)

print(
    f"Selected resolution : "
    f"{target['width']}x{target['height']}"
)

print("Downloading video...")

# -----------------------------------
# Step 5. 다운로드
# -----------------------------------
video_data = requests.get(target["link"])
video_data.raise_for_status()

with open(os.path.join(WORK_DIR, "broll.mp4"), "wb") as f:
    f.write(video_data.content)

print("====================================")
print("Download completed.")
print(f"Selected page       : {random_page}")
print(f"Selected video ID   : {video['id']}")
print(
    f"Resolution          : "
    f"{target['width']}x{target['height']}"
)
print("====================================")