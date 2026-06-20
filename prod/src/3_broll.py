import json
import os
import random
import requests
import subprocess

WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
TEMP_DIR = os.path.join(WORK_DIR, "broll_parts")
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]

FADE_DURATION = 0.3
FALLBACK_QUERY = "calm nature peaceful background"  # 거의 항상 결과 있는 안전 쿼리

os.makedirs(TEMP_DIR, exist_ok=True)
headers = {"Authorization": PEXELS_API_KEY}


def fetch_clip(query, save_path):
    url = f"https://api.pexels.com/videos/search?query={query}&orientation=portrait&per_page=20"
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


def process_scene(i, scene):
    raw_path = os.path.join(TEMP_DIR, f"raw_{i:02d}.mp4")
    out_path = os.path.join(TEMP_DIR, f"part_{i:02d}.mp4")
    duration = scene.get("render_duration", scene["duration"])
    query = scene["visual_query"]

    # 1차: 지정 쿼리
    if fetch_clip(query, raw_path):
        try:
            normalize(raw_path, out_path, duration)
            return {"index": i, "status": "ok", "query_used": query}
        except subprocess.CalledProcessError as e:
            pass  # 정규화 실패 -> fallback으로

    # 2차: fallback 쿼리
    if fetch_clip(FALLBACK_QUERY, raw_path):
        try:
            normalize(raw_path, out_path, duration)
            return {"index": i, "status": "fallback", "query_used": FALLBACK_QUERY}
        except subprocess.CalledProcessError:
            pass

    return {"index": i, "status": "failed", "query_used": query}


with open(os.path.join(WORK_DIR, "scenes_timed.json"), "r", encoding="utf-8") as f:
    scenes = json.load(f)

results = []
for i, scene in enumerate(scenes):
    print(f"[{i}] '{scene['visual_query']}' 처리 중... (목표 {scene.get('render_duration', scene['duration']):.2f}s)")
    r = process_scene(i, scene)
    results.append(r)
    print(f"    -> {r['status']}")

# 상태 로그 저장
with open(os.path.join(WORK_DIR, "broll_status.json"), "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

# 요약 출력
ok = sum(1 for r in results if r["status"] == "ok")
fallback = sum(1 for r in results if r["status"] == "fallback")
failed = [r for r in results if r["status"] == "failed"]

print("\n===== 요약 =====")
print(f"성공: {ok}/{len(scenes)}")
print(f"fallback 사용: {fallback}/{len(scenes)}")
print(f"완전 실패: {len(failed)}/{len(scenes)}")
if failed:
    print("실패한 장면 인덱스:", [r["index"] for r in failed])
    print(">>> 3b_retry_broll.py 실행해서 재시도하세요.")

# concat (실패한 장면은 part 파일이 없으므로 제외)
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

print(f"\nbroll.mp4 생성 완료: {output_path}")

# 임시 파일 정리 (디스크 절약)
for f in os.listdir(TEMP_DIR):
    if f.startswith("raw_") or f.startswith("part_"):
        os.remove(os.path.join(TEMP_DIR, f))
print("임시 파일 정리 완료")