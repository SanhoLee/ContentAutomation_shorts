"""Upload the rendered video and persist job/video linkage."""

from __future__ import annotations

import glob
import importlib.util
import json
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


WORK_DIR = Path(os.environ.get("WORK_DIR", os.path.expanduser("~/brain50/data/work")))
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", os.path.expanduser("~/brain50/data/output")))
ASSETS_DIR = Path(os.environ.get("ASSETS_DIR", os.path.expanduser("~/brain50/data/assets")))
OUTPUT_FILE = os.environ.get("OUTPUT_FILE")


def resolve_video_path() -> Path:
    if OUTPUT_FILE:
        path = Path(OUTPUT_FILE)
        if path.exists():
            return path
        raise RuntimeError(f"OUTPUT_FILE이 존재하지 않습니다: {OUTPUT_FILE}")
    marker_path = WORK_DIR / "output_path.txt"
    if marker_path.exists():
        candidate = Path(marker_path.read_text(encoding="utf-8").strip())
        if candidate.exists():
            return candidate
    output_files = sorted(glob.glob(str(OUTPUT_DIR / "output_*.mp4")))
    if output_files:
        return Path(output_files[-1])
    raise RuntimeError("업로드할 영상이 없습니다.")


def load_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def build_description(video_meta: dict[str, Any]) -> str:
    template = (ASSETS_DIR / "description_template.txt").read_text(encoding="utf-8")
    template = template.replace("{{TOPIC_HASHTAGS}}", str(video_meta.get("hashtags") or ""))
    summary = str(video_meta.get("summary") or "").strip()
    summary_block = f"영상 요약\n{summary}\n\n" if summary else ""
    return f"{video_meta.get('description', '')}\n\n{summary_block}{template}"


def upload_video(video_path: Path, video_meta: dict[str, Any]) -> dict[str, Any]:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    title = str(video_meta["title"])
    if len(title) > 100:
        title = title[:97] + "..."
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
    )
    youtube = build("youtube", "v3", credentials=creds)
    body = {
        "snippet": {
            "title": title,
            "description": build_description(video_meta),
            "categoryId": "27",
            "tags": ["Shorts", "뇌건강", "브레인피프티"],
            "defaultLanguage": "ko",
            "defaultAudioLanguage": "ko",
        },
        "status": {
            "privacyStatus": "private",
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": False,
        },
    }
    request = youtube.videos().insert(
        part="snippet,status", body=body,
        media_body=MediaFileUpload(str(video_path), mimetype="video/mp4", resumable=True),
    )
    return request.execute()


def write_upload_result(
    *,
    job_id: str,
    video_id: str,
    uploaded_at: str | None = None,
    work_dir: Path | None = None,
) -> dict[str, Any]:
    payload = {
        "job_id": job_id,
        "video_id": video_id,
        "uploaded_at": uploaded_at or datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds"),
    }
    target_dir = work_dir or WORK_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "upload_result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def _feedback_module():
    module_path = Path(__file__).with_name("6_youtube_feedback.py")
    spec = importlib.util.spec_from_file_location("brain50_upload_feedback", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("YouTube feedback store를 로드하지 못했습니다.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def persist_upload_link(result: dict[str, Any], video_meta: dict[str, Any]) -> None:
    module = _feedback_module()
    topic_plan = load_json(WORK_DIR / "topic_plan.json", {}) or {}
    plan = topic_plan or {
        "topic": video_meta.get("topic"),
        "main_keyword": video_meta.get("main_keyword"),
        "sub_keywords": video_meta.get("sub_keywords") or [],
        "hook_type": video_meta.get("hook_type"),
        "objective": video_meta.get("objective") or {},
        "content_design": video_meta.get("content_design") or {},
    }
    last_error = None
    for attempt in range(1, 4):
        conn = None
        try:
            conn = module.connect()
            with conn:
                module.link_uploaded_video(
                    conn, job_id=result["job_id"], video_id=result["video_id"],
                    uploaded_at=result["uploaded_at"], topic=str(video_meta.get("topic") or ""),
                    plan=plan,
                )
                objective_type = (plan.get("objective") or {}).get("type")
                module.advance_hypothesis_ttl(conn, objective_type)
            return
        except sqlite3.OperationalError as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.25 * attempt)
        finally:
            if conn is not None:
                conn.close()
    raise RuntimeError(f"업로드는 성공했지만 job/video DB 연결 저장에 실패했습니다: {last_error}")


def run_due_refresh(result: dict[str, Any], video_meta: dict[str, Any]) -> None:
    objective_type = (video_meta.get("objective") or {}).get("type")
    if not objective_type:
        return
    target = WORK_DIR / "strategy_refresh_status.json"
    try:
        from objective_planner import run_due_strategy_review
        payload = run_due_strategy_review(objective_type, job_id=result["job_id"])
    except Exception as exc:
        payload = {"ran": False, "reason": "deferred_after_error", "error": str(exc)[:500]}
        print(f"전략 리프레시는 연기했습니다: {exc}")
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    video_path = resolve_video_path()
    print(f"업로드 대상: {video_path}")
    video_meta = load_json(WORK_DIR / "video_meta.json")
    if not isinstance(video_meta, dict):
        raise RuntimeError(f"video_meta.json을 읽지 못했습니다: {WORK_DIR / 'video_meta.json'}")
    response = upload_video(video_path, video_meta)
    video_id = str(response["id"])
    job_id = os.environ.get("JOB_ID") or WORK_DIR.name
    result = write_upload_result(job_id=job_id, video_id=video_id)
    persist_upload_link(result, video_meta)
    run_due_refresh(result, video_meta)

    print(f"업로드 완료: https://youtube.com/shorts/{video_id}")
    print(f"제목: {video_meta.get('title', '')}")
    print("동영상 언어: ko")
    print("제목 및 설명 언어: ko")
    print("AI 사용: 아니요")
    print("상태: private (확인 후 YouTube Studio에서 공개 전환하세요)")
    print(f"업로드 결과: {WORK_DIR / 'upload_result.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
