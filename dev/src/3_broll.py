import json
import os
import requests
import subprocess

from broll_policy import normalization_filter, select_video

WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
TEMP_DIR = os.path.join(WORK_DIR, "broll_parts")
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]
FADE_DURATION = 0.3
FALLBACK_QUERY = "calm nature peaceful background"

os.makedirs(TEMP_DIR, exist_ok=True)
headers = {"Authorization": PEXELS_API_KEY}
used_video_ids = set()
orientation_history = []


def fetch_clip(query, save_path, min_duration):
    res = requests.get(
        "https://api.pexels.com/videos/search",
        headers=headers,
        params={"query": query, "per_page": 40},
        timeout=30,
    )
    res.raise_for_status()
    selected = select_video(res.json().get("videos", []), min_duration, used_video_ids, orientation_history)
    if not selected:
        return None

    video, target = selected["video"], selected["file"]
    video_id = video.get("id")
    if video_id is not None:
        used_video_ids.add(video_id)
    orientation_history.append(selected["orientation"])
    video_data = requests.get(target["link"], timeout=60)
    video_data.raise_for_status()
    with open(save_path, "wb") as f:
        f.write(video_data.content)
    return {
        "video_id": video_id,
        "video_duration": video.get("duration"),
        "source_width": target["width"],
        "source_height": target["height"],
        "orientation": selected["orientation"],
        "fit_mode": selected["fit_mode"],
        "crop_retention": selected["crop_retention"],
        "duplicate_allowed": selected["duplicate_allowed"],
        "short_allowed": selected["short_allowed"],
    }


def normalize(raw_path, out_path, duration, fit_mode):
    subprocess.run([
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", raw_path,
        "-t", str(duration), "-filter_complex", normalization_filter(fit_mode, duration, FADE_DURATION),
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path,
    ], check=True, capture_output=True)


def process_scene(i, scene):
    raw_path = os.path.join(TEMP_DIR, f"raw_{i:02d}.mp4")
    out_path = os.path.join(TEMP_DIR, f"part_{i:02d}.mp4")
    duration = scene.get("render_duration", scene["duration"])
    query = scene["visual_query"]
    for status, candidate_query in (("ok", query), ("fallback", FALLBACK_QUERY)):
        clip_info = fetch_clip(candidate_query, raw_path, duration)
        if clip_info:
            try:
                normalize(raw_path, out_path, duration, clip_info["fit_mode"])
                return {"index": i, "status": status, "query_used": candidate_query, **clip_info}
            except subprocess.CalledProcessError:
                pass
    return {"index": i, "status": "failed", "query_used": query}


with open(os.path.join(WORK_DIR, "scenes_timed.json"), "r", encoding="utf-8") as f:
    scenes = json.load(f)
results = []
for i, scene in enumerate(scenes):
    print(f"[{i}] '{scene['visual_query']}' 처리 중... (목표 {scene.get('render_duration', scene['duration']):.2f}s)")
    result = process_scene(i, scene)
    results.append(result)
    print(f"    -> {result['status']} ({result.get('orientation', '-')}, {result.get('fit_mode', '-')})")

with open(os.path.join(WORK_DIR, "broll_status.json"), "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

failed = [r for r in results if r["status"] == "failed"]
print("\n===== 요약 =====")
print(f"성공: {len(results) - len(failed)}/{len(scenes)}")
print(f"fallback 사용: {sum(1 for r in results if r['status'] == 'fallback')}/{len(scenes)}")
print("방향:", {kind: sum(1 for r in results if r.get("orientation") == kind) for kind in ("portrait", "square", "landscape")})
if failed:
    print("실패한 장면 인덱스:", [r["index"] for r in failed])
    print(">>> 3b_retry_broll.py 실행해서 재시도하세요.")

normalized_paths = [os.path.join(TEMP_DIR, f"part_{r['index']:02d}.mp4") for r in results if r["status"] != "failed"]
concat_list_path = os.path.join(TEMP_DIR, "concat_list.txt")
with open(concat_list_path, "w", encoding="utf-8") as f:
    for path in normalized_paths:
        f.write(f"file '{path}'\n")
output_path = os.path.join(WORK_DIR, "broll.mp4")
subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path, "-c", "copy", output_path], check=True, capture_output=True)
print(f"\nbroll.mp4 생성 완료: {output_path}")
for name in os.listdir(TEMP_DIR):
    if name.startswith("raw_") or name.startswith("part_"):
        os.remove(os.path.join(TEMP_DIR, name))
print("임시 파일 정리 완료")