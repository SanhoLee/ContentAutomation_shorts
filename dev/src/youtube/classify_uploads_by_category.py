#!/usr/bin/env python3
"""업로드된 유튜브 영상을 research_categories.json의 9개 카테고리로 분류/집계한다.

YouTube Data API로 채널의 전체 업로드 영상(제목+설명)을 가져온 뒤,
목표 기반 플래너가 쓰는 것과 동일한 키워드 매칭(research_signals.match_category)
방식으로 카테고리를 배정한다. DB나 content_features 테이블에 저장된
topic_family가 아니라 실제 YouTube에 올라간 텍스트를 기준으로 분류하므로,
job 메타데이터가 없는 과거 영상도 함께 집계할 수 있다.

인증은 두 가지 방식 중 하나를 사용한다.
  1) 업로드 파이프라인과 동일한 세 값(이미 배포 환경에 export되어 있음):
     YOUTUBE_REFRESH_TOKEN, YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET
  2) 6_youtube_feedback.py의 읽기 전용 OAuth 흐름:
     YOUTUBE_FEEDBACK_TOKEN, YOUTUBE_FEEDBACK_CLIENT_SECRET_FILE

사용법:
  python dev/src/youtube/classify_uploads_by_category.py
  python dev/src/youtube/classify_uploads_by_category.py --list-videos
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

SCRIPT_DIR = Path(__file__).resolve().parent
COMMON_DIR = SCRIPT_DIR.parent / "common"
if str(COMMON_DIR) not in sys.path:
    sys.path.insert(0, str(COMMON_DIR))

from research_signals import CategorySignal, load_category_signals  # noqa: E402

_FEEDBACK_SPEC = importlib.util.spec_from_file_location(
    "brain50_classify_feedback", SCRIPT_DIR / "6_youtube_feedback.py"
)
if _FEEDBACK_SPEC is None or _FEEDBACK_SPEC.loader is None:
    raise RuntimeError("6_youtube_feedback.py를 로드할 수 없습니다.")
feedback = importlib.util.module_from_spec(_FEEDBACK_SPEC)
_FEEDBACK_SPEC.loader.exec_module(feedback)

UNCATEGORIZED = "uncategorized"
UNCATEGORIZED_LABEL = "미분류"


def build_credentials():
    """업로드 파이프라인의 refresh-token 자격증명을 우선 사용하고,
    없으면 6_youtube_feedback.py의 읽기 전용 OAuth 흐름으로 대체한다."""
    import os

    if all(os.environ.get(name) for name in ("YOUTUBE_REFRESH_TOKEN", "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET")):
        from google.oauth2.credentials import Credentials

        return Credentials(
            token=None,
            refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
            client_id=os.environ["YOUTUBE_CLIENT_ID"],
            client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
            token_uri="https://oauth2.googleapis.com/token",
        )
    return feedback.load_credentials()


def classify_text(text: str, signals: dict[str, CategorySignal]) -> str:
    """제목+설명 텍스트에서 키워드가 매칭되는 첫 카테고리를 반환한다.
    research_categories.json에 등록된 순서대로 검사하며, 매칭이 없으면 미분류."""
    lowered = (text or "").lower()
    for signal in signals.values():
        if any(keyword.lower() in lowered for keyword in signal.keywords):
            return signal.category_id
    return UNCATEGORIZED


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="업로드 영상 카테고리별 통계")
    parser.add_argument("--list-videos", action="store_true", help="카테고리별 영상 제목 목록도 출력")
    args = parser.parse_args(argv)

    signals = load_category_signals()
    if not signals:
        print("research_categories.json을 읽을 수 없습니다.", file=sys.stderr)
        return 1

    credentials = build_credentials()
    youtube, _ = feedback.build_services(credentials)

    channel = feedback.fetch_channel(youtube)
    video_ids = feedback.fetch_upload_video_ids(youtube, channel["uploads_playlist_id"])
    videos = feedback.fetch_videos(youtube, video_ids, datetime.now(timezone.utc).isoformat())

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for video in videos:
        text = f"{video.get('title') or ''}\n{video.get('description') or ''}"
        category_id = classify_text(text, signals)
        buckets[category_id].append(video)

    total = len(videos)
    print(f"채널: {channel['title']} / 총 업로드 영상: {total}개\n")

    ordered_ids = list(signals.keys()) + [UNCATEGORIZED]
    for category_id in ordered_ids:
        items = buckets.get(category_id, [])
        label = signals[category_id].label_ko if category_id in signals else UNCATEGORIZED_LABEL
        pct = (len(items) / total * 100.0) if total else 0.0
        print(f"- {label} ({category_id}): {len(items)}개 ({pct:.1f}%)")
        if args.list_videos:
            for video in items:
                print(f"    · {video.get('title')}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
