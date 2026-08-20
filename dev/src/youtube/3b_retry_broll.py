import json
import os
import requests
import subprocess

from broll_policy import (
    load_recent_video_ids,
    next_page_for_query,
    normalization_filter,
    record_query_page,
    record_used_video_ids,
    shot_queries,
    select_video,
)

WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
TEMP_DIR = os.path.join(WORK_DIR, "broll_parts")
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]
headers = {"Authorization": PEXELS_API_KEY}
FADE_DURATION = 0.3
recent_video_ids = load_recent_video_ids()


def fetch_clip(query, save_path, used_video_ids, orientation_history, min_duration):
    page = next_page_for_query(query)
    res = requests.get(
        "https://api.pexels.com/videos/search", headers=headers,
        params={"query": query, "per_page": 40, "page": page}, timeout=30,
    )
    res.raise_for_status()
    selected = select_video(
        res.json().get("videos", []), min_duration, used_video_ids, orientation_history,
        recent_video_ids=recent_video_ids,
    )
    if not selected:
        return None
    video, target = selected["video"], selected["file"]
    video_id = video.get("id")
    if video_id is not None:
        used_video_ids.add(video_id)
    orientation_history.append(selected["orientation"])
    record_query_page(query, page)
    data = requests.get(target["link"], timeout=60)
    data.raise_for_status()
    with open(save_path, "wb") as f:
        f.write(data.content)
    return {
        "video_id": video_id, "video_duration": video.get("duration"),
        "source_width": target["width"], "source_height": target["height"],
        "orientation": selected["orientation"], "fit_mode": selected["fit_mode"],
        "crop_retention": selected["crop_retention"], "duplicate_allowed": selected["duplicate_allowed"],
        "short_allowed": selected["short_allowed"],
    }


def normalize(raw_path, out_path, duration, fit_mode, kenburns=False):
    subprocess.run([
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", raw_path, "-t", str(duration),
        "-filter_complex", normalization_filter(fit_mode, duration, FADE_DURATION, kenburns=kenburns),
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path,
    ], check=True, capture_output=True)


with open(os.path.join(WORK_DIR, "scenes_timed.json"), "r", encoding="utf-8") as f:
    scenes = json.load(f)
with open(os.path.join(WORK_DIR, "broll_status.json"), "r", encoding="utf-8") as f:
    results = json.load(f)

used_video_ids = {shot.get("video_id") for r in results for shot in r["shots"] if shot.get("video_id")}
orientation_history = [
    shot["orientation"] for r in sorted(results, key=lambda item: item["index"])
    for shot in r["shots"] if shot.get("orientation")
]

failed_shots = [
    (r, shot) for r in results if r["status"] != "ok" for shot in r["shots"] if shot["status"] == "failed"
]
if not failed_shots:
    print("재시도할 샷이 없습니다.")
    raise SystemExit(0)

for result, shot in failed_shots:
    index = result["index"]
    scene = scenes[index]
    shot_idx = shot["shot"]
    duration = scene.get("render_duration", scene["duration"])
    planned = shot_queries(scene, len(result["shots"]))
    query = planned[shot_idx] if shot_idx < len(planned) else " ".join(scene["visual_query"].split()[:2])
    raw_path = os.path.join(TEMP_DIR, f"raw_{index:02d}_{shot_idx:02d}.mp4")
    out_path = os.path.join(TEMP_DIR, f"part_{index:02d}_{shot_idx:02d}.mp4")
    print(f"[{index}/{shot_idx}] 재시도: '{query}'")
    clip_info = fetch_clip(query, raw_path, used_video_ids, orientation_history, duration)
    if clip_info:
        normalize(raw_path, out_path, duration, clip_info["fit_mode"], kenburns=clip_info["short_allowed"])
        shot.update({"status": "ok_retry", "query_used": query, "path": out_path, **clip_info})
        print(f"    -> 성공 ({clip_info['orientation']}, {clip_info['fit_mode']})")
    else:
        print("    -> 여전히 실패")

for result in results:
    still_failed = [s for s in result["shots"] if s["status"] == "failed"]
    result["status"] = "failed" if len(still_failed) == len(result["shots"]) else ("partial" if still_failed else "ok")

with open(os.path.join(WORK_DIR, "broll_status.json"), "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
record_used_video_ids([shot.get("video_id") for r in results for shot in r["shots"] if shot.get("video_id")])

normalized_paths = [shot["path"] for r in results for shot in r["shots"] if shot.get("path")]
concat_list_path = os.path.join(TEMP_DIR, "concat_list.txt")
with open(concat_list_path, "w", encoding="utf-8") as f:
    for path in normalized_paths:
        f.write(f"file '{path}'\n")
subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path, "-c", "copy", os.path.join(WORK_DIR, "broll.mp4")], check=True, capture_output=True)
print("broll.mp4 재생성 완료")
