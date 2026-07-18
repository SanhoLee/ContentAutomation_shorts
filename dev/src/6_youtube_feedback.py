#!/usr/bin/env python3
"""YouTube \ucc44\ub110 \uc131\uacfc\ub97c \uc77d\uae30 \uc804\uc6a9\uc73c\ub85c \uc218\uc9d1\u00b7\ubd84\uc11d\ud558\ub294 \ub3c5\ub9bd\ud615 MVP.

\uae30\uc874 \uc81c\uc791/\uc5c5\ub85c\ub4dc \ud30c\uc774\ud504\ub77c\uc778\uacfc DB\ub97c \uacf5\uc720\ud558\uc9c0 \uc54a\ub294\ub2e4.

\uc0ac\uc6a9\ubc95:
  python dev/src/6_youtube_feedback.py sync
  python dev/src/6_youtube_feedback.py report
  python dev/src/6_youtube_feedback.py check-topic "\uc0c8 \uc8fc\uc81c"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


SCOPES = (
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
)
ANALYTICS_METRICS = (
    "views", "engagedViews", "averageViewDuration", "averageViewPercentage",
    "likes", "dislikes", "comments", "shares", "subscribersGained", "subscribersLost",
)
METRIC_COLUMNS = {
    "views": "views",
    "engagedViews": "engaged_views",
    "averageViewDuration": "average_view_duration",
    "averageViewPercentage": "average_view_percentage",
    "likes": "likes",
    "dislikes": "dislikes",
    "comments": "comments",
    "shares": "shares",
    "subscribersGained": "subscribers_gained",
    "subscribersLost": "subscribers_lost",
}
MINIMUM_SAMPLE = 500
ANALYSIS_PERIOD_DAYS = 90
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = SCRIPT_DIR.parent / "data"


def _env_path(name: str, default: Path | None = None) -> Path | None:
    value = os.environ.get(name)
    if value:
        return Path(value).expanduser().resolve()
    return default.resolve() if default else None


def db_path() -> Path:
    return _env_path("YOUTUBE_FEEDBACK_DB", DEFAULT_DATA_DIR / "youtube_feedback.db")  # type: ignore[return-value]


def report_path() -> Path:
    return _env_path("YOUTUBE_FEEDBACK_REPORT", DEFAULT_DATA_DIR / "youtube_report.md")  # type: ignore[return-value]


def strategy_path() -> Path:
    return _env_path("YOUTUBE_FEEDBACK_STRATEGY", DEFAULT_DATA_DIR / "youtube_strategy.json")  # type: ignore[return-value]


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    video_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    published_at TEXT,
    duration_seconds INTEGER,
    view_count INTEGER DEFAULT 0,
    like_count INTEGER DEFAULT 0,
    comment_count INTEGER DEFAULT 0,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analytics (
    video_id TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    views INTEGER DEFAULT 0,
    engaged_views INTEGER,
    average_view_duration REAL,
    average_view_percentage REAL,
    likes INTEGER DEFAULT 0,
    dislikes INTEGER DEFAULT 0,
    comments INTEGER DEFAULT 0,
    shares INTEGER DEFAULT 0,
    subscribers_gained INTEGER DEFAULT 0,
    subscribers_lost INTEGER DEFAULT 0,
    performance_score REAL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (video_id, period_start, period_end)
);
CREATE TABLE IF NOT EXISTS keywords (
    video_id TEXT NOT NULL,
    keyword TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (video_id, keyword, source)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    video_count INTEGER DEFAULT 0,
    error_message TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


STOPWORDS = {
    "\uadf8\ub9ac\uace0", "\uadf8\ub7ec\ub098", "\ud558\uc9c0\ub9cc", "\ub610\ub294", "\ub300\ud55c", "\uc704\ud55c", "\ud1b5\ud574", "\uad00\ub828", "\uc774\uac83", "\uc800\uac83",
    "\uc601\uc0c1", "\uc1fc\uce20", "shorts", "youtube", "\uc720\ud29c\ube0c", "\uc815\ub9d0", "\ubc14\ub85c", "\uc624\ub298", "\uc6b0\ub9ac", "\uc5ec\ub7ec\ubd84",
    "\uc788\ub294", "\uc5c6\ub294", "\ud558\ub294", "\ub418\ub294", "\uc785\ub2c8\ub2e4", "\ud569\ub2c8\ub2e4", "\ud558\uc138\uc694", "\uc788\uc2b5\ub2c8\ub2e4", "\uc5c6\uc2b5\ub2c8\ub2e4",
    "\uac00\uc9c0", "\ubc29\ubc95", "\uc774\uc720", "\ub54c\ubb38", "\ub300\ud574", "\uc5d0\uc11c", "\uc73c\ub85c", "\uc5d0\uac8c", "\ubd80\ud130", "\uae4c\uc9c0", "\ucc98\ub7fc",
}
KOREAN_SUFFIXES = (
    "\uc73c\ub85c\ubd80\ud130", "\uc5d0\uac8c\uc11c\ub294", "\uc5d0\uc11c\ub294", "\uc73c\ub85c\ub294", "\uc774\ub77c\uba74", "\ub77c\uba74", "\uc5d0\uac8c", "\uc5d0\uc11c", "\uc73c\ub85c",
    "\ubd80\ud130", "\uae4c\uc9c0", "\ucc98\ub7fc", "\ubcf4\ub2e4", "\ud558\uace0", "\uc774\uba70", "\uc774\uace0", "\uc758", "\uc740", "\ub294", "\uc774", "\uac00",
    "\uc744", "\ub97c", "\uc5d0", "\uc640", "\uacfc", "\ub3c4", "\ub9cc", "\ub85c",
)


def normalize_keywords(text: str) -> set[str]:
    """\uac00\ubcbc\uc6b4 \ud55c\uad6d\uc5b4 \uc870\uc0ac/\ubd88\uc6a9\uc5b4 \uc815\ub9ac\ub97c \uac70\uce5c \ub2e8\uc5b4 \uc9d1\ud569\uc744 \ubc18\ud658\ud55c\ub2e4."""
    tokens = re.findall(r"[\uac00-\ud7a3A-Za-z]+", (text or "").lower())
    result: set[str] = set()
    for token in tokens:
        if token in STOPWORDS:
            continue
        normalized = token
        if re.search(r"[\uac00-\ud7a3]", token):
            for suffix in KOREAN_SUFFIXES:
                if normalized.endswith(suffix) and len(normalized) - len(suffix) >= 2:
                    normalized = normalized[:-len(suffix)]
                    break
        if len(normalized) >= 2 and normalized not in STOPWORDS:
            result.add(normalized)
    return result


def jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def topic_verdict(similarity: float) -> str:
    if similarity >= 0.55:
        return "\uc911\ubcf5 \uac00\ub2a5\uc131 \ub192\uc74c"
    if similarity >= 0.30:
        return "\uac80\ud1a0"
    return "\ud5c8\uc6a9"


def parse_duration_seconds(value: str | None) -> int | None:
    if not value:
        return None
    match = re.fullmatch(r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not match:
        return None
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def nullable_number(value: Any, cast: type[int] | type[float] = int) -> int | float | None:
    if value is None or value == "":
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def chunks(values: Sequence[str], size: int = 50) -> Iterable[Sequence[str]]:
    for index in range(0, len(values), size):
        yield values[index:index + size]


def load_credentials():
    """\ubcc4\ub3c4 \uc77d\uae30 \ud1a0\ud070\uc744 \ub85c\ub4dc/\uac31\uc2e0\ud55c\ub2e4. \ube44\ubc00 \uac12\uc740 \ucd9c\ub825\ud558\uc9c0 \uc54a\ub294\ub2e4."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise RuntimeError(
            "Google API \ud328\ud0a4\uc9c0\uac00 \uc5c6\uc2b5\ub2c8\ub2e4. google-api-python-client, "
            "google-auth-oauthlib, google-auth-httplib2\ub97c \uc124\uce58\ud558\uc138\uc694."
        ) from exc

    token = _env_path("YOUTUBE_FEEDBACK_TOKEN")
    client_secret = _env_path("YOUTUBE_CLIENT_SECRET")
    if token is None:
        raise RuntimeError("YOUTUBE_FEEDBACK_TOKEN\uc5d0 \ubcc4\ub3c4 \uc77d\uae30 \ud1a0\ud070 \uacbd\ub85c\ub97c \uc9c0\uc815\ud558\uc138\uc694.")
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if client_secret is None or not client_secret.is_file():
            raise RuntimeError("YOUTUBE_CLIENT_SECRET\uc5d0 OAuth client_secret.json \uacbd\ub85c\ub97c \uc9c0\uc815\ud558\uc138\uc694.")
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
        creds = flow.run_local_server(port=0)
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(token, 0o600)
    except OSError:
        pass
    return creds


def build_services(credentials):
    try:
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise RuntimeError("google-api-python-client\uac00 \uc124\uce58\ub418\uc5b4 \uc788\uc9c0 \uc54a\uc2b5\ub2c8\ub2e4.") from exc
    return (
        build("youtube", "v3", credentials=credentials, cache_discovery=False),
        build("youtubeAnalytics", "v2", credentials=credentials, cache_discovery=False),
    )


def fetch_channel(youtube) -> dict[str, str]:
    response = youtube.channels().list(part="contentDetails,snippet", mine=True).execute()
    items = response.get("items") or []
    if not items:
        raise RuntimeError("\uc778\uc99d \uacc4\uc815\uc5d0\uc11c YouTube \ucc44\ub110\uc744 \ucc3e\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4.")
    item = items[0]
    return {
        "channel_id": item.get("id", ""),
        "title": item.get("snippet", {}).get("title", ""),
        "uploads_playlist_id": item.get("contentDetails", {}).get("relatedPlaylists", {}).get("uploads", ""),
    }


def fetch_upload_video_ids(youtube, playlist_id: str) -> list[str]:
    if not playlist_id:
        raise RuntimeError("\ucc44\ub110\uc758 \uc5c5\ub85c\ub4dc \uc7ac\uc0dd\ubaa9\ub85d ID\uac00 \uc5c6\uc2b5\ub2c8\ub2e4.")
    video_ids: list[str] = []
    page_token = None
    while True:
        response = youtube.playlistItems().list(
            part="contentDetails", playlistId=playlist_id, maxResults=50, pageToken=page_token
        ).execute()
        for item in response.get("items") or []:
            video_id = item.get("contentDetails", {}).get("videoId")
            if video_id:
                video_ids.append(video_id)
        page_token = response.get("nextPageToken")
        if not page_token:
            return video_ids


def fetch_videos(youtube, video_ids: Sequence[str], fetched_at: str) -> list[dict[str, Any]]:
    videos: list[dict[str, Any]] = []
    for group in chunks(video_ids):
        response = youtube.videos().list(
            part="snippet,statistics,contentDetails", id=",".join(group), maxResults=50
        ).execute()
        for item in response.get("items") or []:
            try:
                snippet = item.get("snippet") or {}
                stats = item.get("statistics") or {}
                videos.append({
                    "video_id": item["id"],
                    "title": snippet.get("title") or "(\uc81c\ubaa9 \uc5c6\uc74c)",
                    "description": snippet.get("description"),
                    "published_at": snippet.get("publishedAt"),
                    "duration_seconds": parse_duration_seconds((item.get("contentDetails") or {}).get("duration")),
                    "view_count": nullable_number(stats.get("viewCount")),
                    "like_count": nullable_number(stats.get("likeCount")),
                    "comment_count": nullable_number(stats.get("commentCount")),
                    "fetched_at": fetched_at,
                })
            except (KeyError, TypeError):
                continue
    return videos


def _http_status(exc: Exception) -> int | None:
    return getattr(getattr(exc, "resp", None), "status", None)


def _analytics_query(analytics, start: str, end: str, metrics: Sequence[str]) -> dict[str, Any]:
    parameters = {
        "ids": "channel==MINE",
        "startDate": start,
        "endDate": end,
        "metrics": ",".join(metrics),
        "dimensions": "video",
        "maxResults": 200,
    }
    if "views" in metrics:
        parameters["sort"] = "-views"
    return analytics.reports().query(**parameters).execute()


def _merge_analytics_response(target: dict[str, dict[str, Any]], response: dict[str, Any]) -> None:
    headers = [header.get("name") for header in response.get("columnHeaders") or []]
    if "video" not in headers:
        return
    for values in response.get("rows") or []:
        row = dict(zip(headers, values))
        video_id = row.pop("video", None)
        if video_id:
            target.setdefault(str(video_id), {}).update(row)


def fetch_analytics(analytics, start: str, end: str) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """\uc804\uccb4 \uc9c0\ud45c\ub97c \uba3c\uc800 \uc870\ud68c\ud558\uace0 400 \uc624\ub958\uc774\uba74 \uc9c0\uc6d0 \uc9c0\ud45c\ub9cc \uac1c\ubcc4 \uc870\ud68c\ud55c\ub2e4."""
    merged: dict[str, dict[str, Any]] = {}
    try:
        response = _analytics_query(analytics, start, end, ANALYTICS_METRICS)
        _merge_analytics_response(merged, response)
        return merged, list(ANALYTICS_METRICS)
    except Exception as exc:
        if _http_status(exc) != 400:
            raise

    supported: list[str] = []
    for metric in ANALYTICS_METRICS:
        try:
            response = _analytics_query(analytics, start, end, (metric,))
        except Exception as exc:
            if _http_status(exc) == 400:
                continue
            raise
        supported.append(metric)
        _merge_analytics_response(merged, response)
    if not supported:
        raise RuntimeError("\ud604\uc7ac \ucc44\ub110/\uae30\uac04\uc5d0\uc11c \uc9c0\uc6d0\ub418\ub294 Analytics \uc9c0\ud45c\ub97c \ucc3e\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4.")
    return merged, supported


VIDEO_UPSERT = """
INSERT INTO videos (
    video_id, title, description, published_at, duration_seconds,
    view_count, like_count, comment_count, fetched_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(video_id) DO UPDATE SET
    title=excluded.title,
    description=excluded.description,
    published_at=excluded.published_at,
    duration_seconds=COALESCE(excluded.duration_seconds, videos.duration_seconds),
    view_count=COALESCE(excluded.view_count, videos.view_count),
    like_count=COALESCE(excluded.like_count, videos.like_count),
    comment_count=COALESCE(excluded.comment_count, videos.comment_count),
    fetched_at=excluded.fetched_at
"""

ANALYTICS_UPSERT = """
INSERT INTO analytics (
    video_id, period_start, period_end, views, engaged_views,
    average_view_duration, average_view_percentage, likes, dislikes,
    comments, shares, subscribers_gained, subscribers_lost,
    performance_score, fetched_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
ON CONFLICT(video_id, period_start, period_end) DO UPDATE SET
    views=COALESCE(excluded.views, analytics.views),
    engaged_views=COALESCE(excluded.engaged_views, analytics.engaged_views),
    average_view_duration=COALESCE(excluded.average_view_duration, analytics.average_view_duration),
    average_view_percentage=COALESCE(excluded.average_view_percentage, analytics.average_view_percentage),
    likes=COALESCE(excluded.likes, analytics.likes),
    dislikes=COALESCE(excluded.dislikes, analytics.dislikes),
    comments=COALESCE(excluded.comments, analytics.comments),
    shares=COALESCE(excluded.shares, analytics.shares),
    subscribers_gained=COALESCE(excluded.subscribers_gained, analytics.subscribers_gained),
    subscribers_lost=COALESCE(excluded.subscribers_lost, analytics.subscribers_lost),
    fetched_at=excluded.fetched_at
"""


def store_videos(conn: sqlite3.Connection, videos: Sequence[dict[str, Any]]) -> None:
    for video in videos:
        conn.execute(VIDEO_UPSERT, (
            video["video_id"], video["title"], video.get("description"), video.get("published_at"),
            video.get("duration_seconds"), video.get("view_count"), video.get("like_count"),
            video.get("comment_count"), video["fetched_at"],
        ))
        conn.execute("DELETE FROM keywords WHERE video_id=?", (video["video_id"],))
        for source in ("title", "description"):
            for keyword in normalize_keywords(video.get(source) or ""):
                conn.execute(
                    "INSERT OR IGNORE INTO keywords (video_id, keyword, source) VALUES (?, ?, ?)",
                    (video["video_id"], keyword, source),
                )


def store_analytics(
    conn: sqlite3.Connection,
    rows: dict[str, dict[str, Any]],
    period_start: str,
    period_end: str,
    fetched_at: str,
) -> None:
    for video_id, source in rows.items():
        values = {
            column: nullable_number(source.get(metric), float if "average" in column else int)
            for metric, column in METRIC_COLUMNS.items()
        }
        conn.execute(ANALYTICS_UPSERT, (
            video_id, period_start, period_end,
            values["views"], values["engaged_views"], values["average_view_duration"],
            values["average_view_percentage"], values["likes"], values["dislikes"],
            values["comments"], values["shares"], values["subscribers_gained"],
            values["subscribers_lost"], fetched_at,
        ))


def _safe_rate(numerator: float | int | None, denominator: float | int | None) -> float | None:
    if numerator is None:
        return None
    return float(numerator) / max(float(denominator or 0), 1.0)


def calculate_performance_scores(conn: sqlite3.Connection, period_start: str, period_end: str) -> None:
    rows = conn.execute(
        "SELECT * FROM analytics WHERE period_start=? AND period_end=?",
        (period_start, period_end),
    ).fetchall()
    if not rows:
        return

    calculated: list[tuple[str, dict[str, float | None]]] = []
    for row in rows:
        denominator = max(row["engaged_views"] or 0, row["views"] or 0, 1)
        net_subscribers = None
        if row["subscribers_gained"] is not None or row["subscribers_lost"] is not None:
            net_subscribers = (row["subscribers_gained"] or 0) - (row["subscribers_lost"] or 0)
        calculated.append((row["video_id"], {
            "average_view_percentage": nullable_number(row["average_view_percentage"], float),
            "share_rate": _safe_rate(row["shares"], denominator),
            "like_rate": _safe_rate(row["likes"], denominator),
            "subscriber_rate": _safe_rate(net_subscribers, denominator),
            "comment_rate": _safe_rate(row["comments"], denominator),
        }))

    weights = {
        "average_view_percentage": 0.40,
        "share_rate": 0.25,
        "like_rate": 0.15,
        "subscriber_rate": 0.15,
        "comment_rate": 0.05,
    }
    medians: dict[str, float | None] = {}
    for metric in weights:
        values = [float(metrics[metric]) for _, metrics in calculated if metrics[metric] is not None]
        median = statistics.median(values) if values else None
        medians[metric] = median if median is not None and median > 0 else None

    for video_id, metrics in calculated:
        components = [
            (weights[metric], float(value) / medians[metric])
            for metric, value in metrics.items()
            if value is not None and medians[metric] is not None
        ]
        total_weight = sum(weight for weight, _ in components)
        score = sum(weight * relative for weight, relative in components) / total_weight if total_weight else None
        conn.execute(
            "UPDATE analytics SET performance_score=? WHERE video_id=? AND period_start=? AND period_end=?",
            (score, video_id, period_start, period_end),
        )


def classify_api_error(exc: Exception) -> str:
    status = _http_status(exc)
    text = str(exc).lower()
    if status in (401, 403) and any(word in text for word in ("quota", "dailylimit", "ratelimit")):
        return "\ud560\ub2f9\ub7c9 \ucd08\uacfc"
    if status in (401, 403):
        return "\uc778\uc99d/\uad8c\ud55c \uc2e4\ud328"
    if status == 400:
        return "\uc9c0\uc6d0\ud558\uc9c0 \uc54a\ub294 \uc694\uccad \ub610\ub294 \uc9c0\ud45c"
    return "API/\ub3d9\uae30\ud654 \uc2e4\ud328"


def cmd_sync(_args: argparse.Namespace) -> int:
    conn = connect()
    cursor = conn.execute("INSERT INTO sync_runs (started_at, status) VALUES (?, 'running')", (iso_now(),))
    run_id = cursor.lastrowid
    conn.commit()
    try:
        credentials = load_credentials()
        youtube, analytics = build_services(credentials)
        channel = fetch_channel(youtube)
        video_ids = fetch_upload_video_ids(youtube, channel["uploads_playlist_id"])
        fetched_at = iso_now()
        videos = fetch_videos(youtube, video_ids, fetched_at)

        end_date = date.today() - timedelta(days=2)
        start_date = end_date - timedelta(days=ANALYSIS_PERIOD_DAYS - 1)
        analytics_rows, supported_metrics = fetch_analytics(
            analytics, start_date.isoformat(), end_date.isoformat()
        )

        with conn:
            store_videos(conn, videos)
            store_analytics(conn, analytics_rows, start_date.isoformat(), end_date.isoformat(), fetched_at)
            calculate_performance_scores(conn, start_date.isoformat(), end_date.isoformat())
            conn.execute(
                "UPDATE sync_runs SET finished_at=?, status='success', video_count=?, error_message=NULL WHERE run_id=?",
                (iso_now(), len(videos), run_id),
            )
        print(f"\ucc44\ub110: {channel['title']} ({channel['channel_id']})")
        print(f"\ub3d9\uae30\ud654 \uc644\ub8cc: \uc601\uc0c1 {len(videos)}\uac1c, Analytics \uc601\uc0c1 {len(analytics_rows)}\uac1c")
        print(f"\uc9c0\uc6d0 Analytics \uc9c0\ud45c: {', '.join(supported_metrics)}")
        print(f"DB: {db_path()}")
        return 0
    except Exception as exc:
        category = classify_api_error(exc)
        safe_message = f"{category}: {type(exc).__name__}"
        with conn:
            conn.execute(
                "UPDATE sync_runs SET finished_at=?, status='failed', error_message=? WHERE run_id=?",
                (iso_now(), safe_message, run_id),
            )
        print(f"\ub3d9\uae30\ud654 \uc2e4\ud328: {safe_message}", file=sys.stderr)
        return 1
    finally:
        conn.close()


LATEST_ANALYTICS_SQL = """
WITH latest AS (
    SELECT a.*,
           ROW_NUMBER() OVER (PARTITION BY video_id ORDER BY period_end DESC, fetched_at DESC) AS row_number
    FROM analytics a
)
SELECT v.*, l.period_start, l.period_end, l.views, l.engaged_views,
       l.average_view_percentage, l.performance_score,
       l.likes AS analytics_likes, l.comments AS analytics_comments,
       l.shares, l.subscribers_gained, l.subscribers_lost
FROM videos v
LEFT JOIN latest l ON l.video_id=v.video_id AND l.row_number=1
"""


def _low_confidence(row: sqlite3.Row | dict[str, Any]) -> bool:
    return max(row["engaged_views"] or 0, row["views"] or 0) < MINIMUM_SAMPLE


def build_report_data(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(LATEST_ANALYTICS_SQL).fetchall()
    scored = sorted(
        (row for row in rows if row["performance_score"] is not None),
        key=lambda row: row["performance_score"],
        reverse=True,
    )
    top_videos = [{
        "video_id": row["video_id"],
        "title": row["title"],
        "performance_score": round(row["performance_score"], 4),
        "low_confidence": _low_confidence(row),
    } for row in scored[:10]]

    keyword_rows = conn.execute("""
        WITH latest AS (
            SELECT a.*, ROW_NUMBER() OVER (
                PARTITION BY video_id ORDER BY period_end DESC, fetched_at DESC
            ) AS row_number
            FROM analytics a
        )
        SELECT k.keyword, AVG(l.performance_score) AS avg_score, COUNT(DISTINCT k.video_id) AS video_count
        FROM keywords k JOIN latest l ON l.video_id=k.video_id AND l.row_number=1
        WHERE l.performance_score IS NOT NULL
        GROUP BY k.keyword
        ORDER BY avg_score DESC, video_count DESC, k.keyword
        LIMIT 15
    """).fetchall()
    recent = sorted(rows, key=lambda row: row["published_at"] or "", reverse=True)[:10]
    last_sync = conn.execute(
        "SELECT finished_at, video_count FROM sync_runs WHERE status='success' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    return {
        "generated_at": iso_now(),
        "analysis_period_days": ANALYSIS_PERIOD_DAYS,
        "video_count": len(rows),
        "minimum_sample": MINIMUM_SAMPLE,
        "last_successful_sync": last_sync["finished_at"] if last_sync else None,
        "top_videos": top_videos,
        "low_sample_videos": [{
            "video_id": row["video_id"],
            "title": row["title"],
            "sample": max(row["engaged_views"] or 0, row["views"] or 0),
        } for row in scored if _low_confidence(row)],
        "preferred_keywords": [row["keyword"] for row in keyword_rows],
        "keyword_performance": [{
            "keyword": row["keyword"],
            "average_score": round(row["avg_score"], 4),
            "video_count": row["video_count"],
        } for row in keyword_rows],
        "recent_topics": [row["title"] for row in recent],
        "notes": [
            "\uc870\ud68c\uc218 \ub2e8\ub3c5 \uae30\uc900\uc73c\ub85c \ubc29\ud5a5\uc744 \ubcc0\uacbd\ud558\uc9c0 \uc54a\uc74c",
            "\ubd84\uc11d \uacb0\uacfc\ub294 \uae30\uc874 \uc81c\uc791 \ud30c\uc774\ud504\ub77c\uc778\uc5d0 \uc790\ub3d9 \uc801\uc6a9\ud558\uc9c0 \uc54a\uc74c",
            "\uc911\ubcf5 \ud310\uc815\uc740 \uc0ac\uc804 \uacbd\uace0\uc774\uba70 \uc790\ub3d9 \ucc28\ub2e8\ud558\uc9c0 \uc54a\uc74c",
        ],
    }


def render_markdown(data: dict[str, Any]) -> str:
    last_sync = data["last_successful_sync"] or "\uc5c6\uc74c"
    lines = [
        "# YouTube \ucc44\ub110 \ud53c\ub4dc\ubc31 \ubcf4\uace0\uc11c", "",
        f"- \uc0dd\uc131 \uc2dc\uac01: {data['generated_at']}",
        f"- \ub9c8\uc9c0\ub9c9 \uc815\uc0c1 \ub3d9\uae30\ud654: {last_sync}",
        f"- \ubd84\uc11d \uc601\uc0c1 \uc218: {data['video_count']}\uac1c",
        f"- \ubd84\uc11d \uae30\uac04: \ucd5c\uadfc {data['analysis_period_days']}\uc77c",
        f"- \ucd5c\uc18c \uc2e0\ub8b0 \ud45c\ubcf8: {data['minimum_sample']}\ud68c", "",
        "## \uc0c1\ub300 \uc131\uacfc \uc0c1\uc704 \uc601\uc0c1", "",
        "| \uc21c\uc704 | \uc81c\ubaa9 | \uc131\uacfc \uc810\uc218 | \uc2e0\ub8b0\ub3c4 |",
        "|---:|---|---:|---|",
    ]
    if data["top_videos"]:
        for index, video in enumerate(data["top_videos"], 1):
            title = video["title"].replace("|", "\\|").replace("\n", " ")
            confidence = "\ub0ae\uc74c" if video["low_confidence"] else "\uc77c\ubc18"
            lines.append(f"| {index} | {title} | {video['performance_score']:.3f} | {confidence} |")
    else:
        lines.append("| - | Analytics \uc131\uacfc \ub370\uc774\ud130 \uc5c6\uc74c | - | - |")

    lines.extend(["", "## \ucd5c\uc18c \ud45c\ubcf8 \ubbf8\ub2ec \uc601\uc0c1", ""])
    if data["low_sample_videos"]:
        for video in data["low_sample_videos"]:
            lines.append(f"- {video['title']} \u2014 \ud45c\ubcf8 {video['sample']:,}\ud68c")
    else:
        lines.append("- \uc5c6\uc74c")

    lines.extend(["", "## \uc131\uacfc\uac00 \ub192\uc740 \ud0a4\uc6cc\ub4dc", ""])
    if data["keyword_performance"]:
        for keyword in data["keyword_performance"]:
            lines.append(
                f"- {keyword['keyword']} \u2014 \ud3c9\uade0 \uc810\uc218 {keyword['average_score']:.3f}, "
                f"\uc601\uc0c1 {keyword['video_count']}\uac1c"
            )
    else:
        lines.append("- \ubd84\uc11d \uac00\ub2a5\ud55c \ud0a4\uc6cc\ub4dc \uc5c6\uc74c")

    lines.extend(["", "## \ucd5c\uadfc \uc81c\uc791 \uc8fc\uc81c", ""])
    if data["recent_topics"]:
        lines.extend(f"- {topic}" for topic in data["recent_topics"])
    else:
        lines.append("- \uc5c6\uc74c")
    lines.extend(["", "## \ud574\uc11d \uc8fc\uc758\uc0ac\ud56d", ""])
    lines.extend(f"- {note}" for note in data["notes"])
    lines.append("")
    return "\n".join(lines)


def write_reports(conn: sqlite3.Connection) -> tuple[Path, Path, dict[str, Any]]:
    data = build_report_data(conn)
    markdown_target = report_path()
    json_target = strategy_path()
    markdown_target.parent.mkdir(parents=True, exist_ok=True)
    json_target.parent.mkdir(parents=True, exist_ok=True)
    markdown_target.write_text(render_markdown(data), encoding="utf-8")
    json_target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return markdown_target, json_target, data


def cmd_report(_args: argparse.Namespace) -> int:
    conn = connect()
    try:
        markdown_target, json_target, data = write_reports(conn)
    finally:
        conn.close()
    print(f"\ubcf4\uace0\uc11c \uc0dd\uc131 \uc644\ub8cc: \uc601\uc0c1 {data['video_count']}\uac1c")
    print(f"Markdown: {markdown_target}")
    print(f"JSON: {json_target}")
    return 0


def find_similar_topics(conn: sqlite3.Connection, topic: str, limit: int = 5) -> list[dict[str, Any]]:
    topic_words = normalize_keywords(topic)
    video_words: dict[str, set[str]] = defaultdict(set)
    for row in conn.execute("SELECT video_id, keyword FROM keywords"):
        video_words[row["video_id"]].add(row["keyword"])
    videos = {
        row["video_id"]: row
        for row in conn.execute("SELECT video_id, title, published_at FROM videos")
    }
    results = []
    for video_id, words in video_words.items():
        row = videos.get(video_id)
        if row is None:
            continue
        similarity = jaccard(topic_words, words)
        results.append({
            "video_id": video_id,
            "title": row["title"],
            "published_at": row["published_at"],
            "similarity": similarity,
            "verdict": topic_verdict(similarity),
            "common_keywords": sorted(topic_words & words),
        })
    return sorted(
        results,
        key=lambda item: (item["similarity"], item["published_at"] or ""),
        reverse=True,
    )[:limit]


def cmd_check_topic(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        results = find_similar_topics(conn, args.topic)
    finally:
        conn.close()
    print(f"\uc785\ub825 \uc8fc\uc81c: {args.topic}")
    if not normalize_keywords(args.topic):
        print("\ud310\uc815: \uac80\ud1a0 (\ube44\uad50\ud560 \uc720\ud6a8 \ud0a4\uc6cc\ub4dc\uac00 \uc5c6\uc2b5\ub2c8\ub2e4.)")
        return 0
    overall = topic_verdict(results[0]["similarity"] if results else 0.0)
    print(f"\ud310\uc815: {overall}")
    print("\uc720\uc0ac \uc601\uc0c1:")
    if not results:
        print("  \uc800\uc7a5\ub41c \uc601\uc0c1\uc774 \uc5c6\uc2b5\ub2c8\ub2e4.")
        return 0
    for index, result in enumerate(results, 1):
        common = ", ".join(result["common_keywords"]) or "\uc5c6\uc74c"
        print(
            f"  {index}. {result['title']} \u2014 {result['similarity']:.1%} "
            f"[{result['verdict']}] (\uacf5\ud1b5: {common})"
        )
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="YouTube API \ud53c\ub4dc\ubc31 MVP")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("sync", help="\ucc44\ub110 \uc601\uc0c1\uacfc \ucd5c\uadfc Analytics\ub97c \ub3d9\uae30\ud654")
    subparsers.add_parser("report", help="Markdown/JSON \ubcf4\uace0\uc11c \uc0dd\uc131")
    topic_parser = subparsers.add_parser("check-topic", help="\uae30\uc874 \uc601\uc0c1\uacfc \uc0c8 \uc8fc\uc81c\uc758 \uc911\ubcf5 \uac00\ub2a5\uc131 \ud655\uc778")
    topic_parser.add_argument("topic", help="\uac80\uc0ac\ud560 \uc0c8 \uc8fc\uc81c")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    commands = {"sync": cmd_sync, "report": cmd_report, "check-topic": cmd_check_topic}
    return commands[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
