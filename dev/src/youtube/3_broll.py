import json
import os
import random
import requests
import subprocess

from broll_policy import (
    BROLL_KENBURNS_RATIO,
    load_recent_video_ids,
    next_page_for_query,
    normalization_filter,
    record_query_page,
    record_used_video_ids,
    shot_durations_for_scene,
    shot_queries,
    select_video,
)

WORK_DIR = os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work"))
TEMP_DIR = os.path.join(WORK_DIR, "broll_parts")
PEXELS_API_KEY = os.environ["PEXELS_API_KEY"]
FADE_DURATION = 0.3
# A single fixed fallback made every failed scene across every video pull from
# the same result page. Rotate over generic, motion-carrying queries instead.
FALLBACK_QUERIES = (
    "senior couple walking outdoors",
    "woman cooking healthy kitchen",
    "elderly people talking together",
    "morning routine sunlight home",
    "person stretching living room",
    "walking city street daytime",
    "hands preparing food table",
    "family gathering meal home",
)

os.makedirs(TEMP_DIR, exist_ok=True)
headers = {"Authorization": PEXELS_API_KEY}
used_video_ids = set()
orientation_history = []
recent_video_ids = load_recent_video_ids()
fallback_pool = list(FALLBACK_QUERIES)
random.shuffle(fallback_pool)
fallback_counter = 0


def next_fallback_query():
    global fallback_counter
    query = fallback_pool[fallback_counter % len(fallback_pool)]
    fallback_counter += 1
    return query


def fetch_clip(query, save_path, min_duration):
    page = next_page_for_query(query)
    res = requests.get(
        "https://api.pexels.com/videos/search",
        headers=headers,
        params={"query": query, "per_page": 40, "page": page},
        timeout=30,
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


def normalize(raw_path, out_path, duration, fit_mode, kenburns=False):
    subprocess.run([
        "ffmpeg", "-y", "-stream_loop", "-1", "-i", raw_path,
        "-t", str(duration), "-filter_complex",
        normalization_filter(fit_mode, duration, FADE_DURATION, kenburns=kenburns),
        "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", out_path,
    ], check=True, capture_output=True)


def process_shot(scene_idx, shot_idx, query, duration):
    raw_path = os.path.join(TEMP_DIR, f"raw_{scene_idx:02d}_{shot_idx:02d}.mp4")
    out_path = os.path.join(TEMP_DIR, f"part_{scene_idx:02d}_{shot_idx:02d}.mp4")
    attempts = ([("ok", query)] if query else []) + [("fallback", next_fallback_query())]
    for status, candidate_query in attempts:
        clip_info = fetch_clip(candidate_query, raw_path, duration)
        if not clip_info:
            continue
        # A looped short source always gets the zoom; otherwise it's applied
        # to a random slice of clips so cuts don't all look mechanically alike.
        kenburns = clip_info["short_allowed"] or random.random() < BROLL_KENBURNS_RATIO
        try:
            normalize(raw_path, out_path, duration, clip_info["fit_mode"], kenburns=kenburns)
            return {
                "shot": shot_idx, "status": status, "query_used": candidate_query,
                "path": out_path, "kenburns": kenburns, **clip_info,
            }
        except subprocess.CalledProcessError:
            pass
    return {"shot": shot_idx, "status": "failed", "query_used": query or "", "path": None}


def process_scene(i, scene):
    duration = scene.get("render_duration", scene["duration"])
    durations = shot_durations_for_scene(scene, duration)
    queries = shot_queries(scene, len(durations))
    shots = [
        process_shot(i, j, query, shot_duration)
        for j, (query, shot_duration) in enumerate(zip(queries, durations))
    ]
    failed = [s for s in shots if s["status"] == "failed"]
    if not failed:
        status = "ok"
    elif len(failed) == len(shots):
        status = "failed"
    else:
        status = "partial"
    return {"index": i, "status": status, "shots": shots}


with open(os.path.join(WORK_DIR, "scenes_timed.json"), "r", encoding="utf-8") as f:
    scenes = json.load(f)
results = []
for i, scene in enumerate(scenes):
    target = scene.get("render_duration", scene["duration"])
    print(f"[{i}] '{scene.get('visual_query', '-')}' 처리 중... (목표 {target:.2f}s, {len(shot_durations_for_scene(scene, target))}샷)")
    result = process_scene(i, scene)
    results.append(result)
    shot_summary = ", ".join(f"{s['status']}/{s.get('orientation', '-')}" for s in result["shots"])
    print(f"    -> {result['status']} [{shot_summary}]")

with open(os.path.join(WORK_DIR, "broll_status.json"), "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)

all_video_ids = [
    shot.get("video_id") for r in results for shot in r["shots"] if shot.get("video_id")
]
record_used_video_ids(all_video_ids)

failed_scenes = [r for r in results if r["status"] == "failed"]
partial_scenes = [r for r in results if r["status"] == "partial"]
all_shots = [shot for r in results for shot in r["shots"]]
print("\n===== 요약 =====")
print(f"씬: {len(results) - len(failed_scenes)}/{len(results)} 성공 (부분 실패 {len(partial_scenes)})")
print(f"샷: {sum(1 for s in all_shots if s['status'] != 'failed')}/{len(all_shots)} 성공, fallback {sum(1 for s in all_shots if s['status'] == 'fallback')}")
print("방향:", {kind: sum(1 for s in all_shots if s.get("orientation") == kind) for kind in ("portrait", "square", "landscape")})
if failed_scenes or partial_scenes:
    print("실패/부분 실패 씬 인덱스:", [r["index"] for r in failed_scenes + partial_scenes])
    print(">>> 3b_retry_broll.py 실행해서 재시도하세요.")

normalized_paths = [shot["path"] for r in results for shot in r["shots"] if shot.get("path")]
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
