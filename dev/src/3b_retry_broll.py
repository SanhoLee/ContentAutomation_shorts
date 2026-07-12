import json
import os
import requests
import subprocess

from broll_policy import normalization_filter, select_video

WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
TEMP_DIR = os.path.join(WORK_DIR, "broll_parts")
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]
headers = {"Authorization": PEXELS_API_KEY}


def fetch_clip(query, save_path, used_video_ids, orientation_history, min_duration):
    res = requests.get("https://api.pexels.com/videos/search", headers=headers, params={"query": query, "per_page": 40}, timeout=30)
    res.raise_for_status()
    selected = select_video(res.json().get("videos", []), min_duration, used_video_ids, orientation_history)
    if not selected:
        return None
    video, target = selected["video"], selected["file"]
    video_id = video.get("id")
    if video_id is not None:
        used_video_ids.add(video_id)
    orientation_history.append(selected["orientation"])
    data = requests.get(target["link"], timeout=60)
    data.raise_for_status()
    with open(save_path, "wb") as f:
        f.write(data.content)
    return {"video_id": video_id, "video_duration": video.get("duration"), "source_width": target["width"], "source_height": target["height"], "orientation": selected["orientation"], "fit_mode": selected["fit_mode"], "crop_retention": selected["crop_retention"], "duplicate_allowed": selected["duplicate_allowed"], "short_allowed": selected["short_allowed"]}


def normalize(raw_path, out_path, duration, fit_mode):
    subprocess.run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", raw_path, "-t", str(duration), "-filter_complex", normalization_filter(fit_mode, duration), "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path], check=True, capture_output=True)


with open(os.path.join(WORK_DIR, "scenes_timed.json"), "r", encoding="utf-8") as f:
    scenes = json.load(f)
with open(os.path.join(WORK_DIR, "broll_status.json"), "r", encoding="utf-8") as f:
    results = json.load(f)
failed = [r for r in results if r["status"] == "failed"]
used_video_ids = {r.get("video_id") for r in results if r.get("video_id")}
orientation_history = [r["orientation"] for r in sorted(results, key=lambda item: item["index"]) if r.get("orientation")]
if not failed:
    print("재시도할 장면이 없습니다.")
    raise SystemExit(0)

for result in failed:
    index = result["index"]
    scene = scenes[index]
    duration = scene.get("render_duration", scene["duration"])
    query = " ".join(scene["visual_query"].split()[:2])
    raw_path = os.path.join(TEMP_DIR, f"raw_{index:02d}.mp4")
    out_path = os.path.join(TEMP_DIR, f"part_{index:02d}.mp4")
    print(f"[{index}] 재시도: '{query}'")
    clip_info = fetch_clip(query, raw_path, used_video_ids, orientation_history, duration)
    if clip_info:
        normalize(raw_path, out_path, duration, clip_info["fit_mode"])
        result.update({"status": "ok_retry", "query_used": query, **clip_info})
        print(f"    -> 성공 ({clip_info['orientation']}, {clip_info['fit_mode']})")
    else:
        print("    -> 여전히 실패")

with open(os.path.join(WORK_DIR, "broll_status.json"), "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
normalized_paths = [os.path.join(TEMP_DIR, f"part_{r['index']:02d}.mp4") for r in results if r["status"] != "failed"]
concat_list_path = os.path.join(TEMP_DIR, "concat_list.txt")
with open(concat_list_path, "w", encoding="utf-8") as f:
    for path in normalized_paths:
        f.write(f"file '{path}'\n")
subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_list_path, "-c", "copy", os.path.join(WORK_DIR, "broll.mp4")], check=True, capture_output=True)
print("broll.mp4 재생성 완료")