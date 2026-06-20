import json
import os
import random
import requests
import subprocess

WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
TEMP_DIR = os.path.join(WORK_DIR, "broll_parts")
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]
FADE_DURATION = 0.3

headers = {"Authorization": PEXELS_API_KEY}


def fetch_clip(query, save_path):
    url = f"https://api.pexels.com/videos/search?query={query}&orientation=portrait&per_page=15"
    res = requests.get(url, headers=headers)
    res.raise_for_status()
    videos = res.json().get("videos", [])
    if not videos:
        return False
    video = random.choice(videos)
    portrait = [v for v in video["video_files"] if v["width"] < v["height"]]
    candidates = portrait or video["video_files"]
    target = random.choice(candidates)
    video_data = requests.get(target["link"])
    video_data.raise_for_status()
    with open(save_path, "wb") as f:
        f.write(video_data.content)
    return True


def normalize(raw_path, out_path, duration):
    fade_out_start = max(duration - FADE_DURATION, 0)
    vf = (
        "scale=1080:1920:force_original_aspect_ratio=increase,"
        "crop=1080:1920,fps=30,"
        f"fade=t=in:st=0:d={FADE_DURATION},"
        f"fade=t=out:st={fade_out_start}:d={FADE_DURATION}"
    )
    subprocess.run([
        "ffmpeg", "-y",
        "-stream_loop", "-1", "-i", raw_path,
        "-t", str(duration),
        "-vf", vf,
        "-an",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        out_path
    ], check=True, capture_output=True)


with open(os.path.join(WORK_DIR, "scenes_timed.json"), "r", encoding="utf-8") as f:
    scenes = json.load(f)

with open(os.path.join(WORK_DIR, "broll_status.json"), "r", encoding="utf-8") as f:
    results = json.load(f)

failed = [r for r in results if r["status"] == "failed"]

if not failed:
    print("재시도할 장면이 없습니다.")
    exit()

print(f"재시도 대상: {len(failed)}개 -> {[r['index'] for r in failed]}")

for r in failed:
    i = r["index"]
    scene = scenes[i]
    duration = scene.get("render_duration", scene["duration"])

    # 쿼리를 단순화해서 재시도 (앞 2단어만)
    simplified_query = " ".join(scene["visual_query"].split()[:2])
    raw_path = os.path.join(TEMP_DIR, f"raw_{i:02d}.mp4")
    out_path = os.path.join(TEMP_DIR, f"part_{i:02d}.mp4")

    print(f"[{i}] 재시도: '{simplified_query}'")

    if fetch_clip(simplified_query, raw_path):
        normalize(raw_path, out_path, duration)
        r["status"] = "ok_retry"
        r["query_used"] = simplified_query
        print("    -> 성공")
    else:
        print("    -> 여전히 실패")

# 상태 업데이트
with open(os.path.join(WORK_DIR, "broll_status.json"), "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

# 재concat
still_failed = [r for r in results if r["status"] == "failed"]
if still_failed:
    print(f"\n여전히 실패: {[r['index'] for r in still_failed]} - 이 장면들은 영상에서 제외됩니다.")

normalized_paths = [
    os.path.join(TEMP_DIR, f"part_{r['index']:02d}.mp4")
    for r in results if r["status"] != "failed"
]

concat_list_path = os.path.join(TEMP_DIR, "concat_list.txt")
with open(concat_list_path, "w", encoding="utf-8") as f:
    for p in normalized_paths:
        f.write(f"file '{p}'\n")

output_path = os.path.join(WORK_DIR, "broll.mp4")
subprocess.run([
    "ffmpeg", "-y", "-f", "concat", "-safe", "0",
    "-i", concat_list_path, "-c", "copy",
    output_path
], check=True, capture_output=True)

print(f"\nbroll.mp4 재생성 완료: {output_path}")