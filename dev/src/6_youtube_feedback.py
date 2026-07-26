#!/usr/bin/env python3
"""YouTube 채널 성과 저장소와 목표 기반 분석 기능.

기존 read-only 피드백 DB를 제작 작업, 성과 스냅샷, 전략 가설과
공유하도록 확장한다. 업로드 API 권한과 성과 수집 권한은 계속 분리한다.

\uc0ac\uc6a9\ubc95:
  python dev/src/6_youtube_feedback.py sync
  python dev/src/6_youtube_feedback.py report
  python dev/src/6_youtube_feedback.py check-topic "\uc0c8 \uc8fc\uc81c"
  python dev/src/6_youtube_feedback.py guide "\uc0c8 \uc8fc\uc81c" --strictness balanced
"""

from __future__ import annotations

import argparse
import json
import math
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
PERFORMANCE_WEIGHTS = {
    "shorts_feed_engagement_rate": 0.25,
    "average_view_percentage": 0.30,
    "subscriber_rate": 0.25,
    "share_rate": 0.10,
    "like_rate": 0.05,
    "comment_rate": 0.05,
}
ANALYSIS_PERIOD_DAYS = 90
MATURITY_LEARNING_VIDEOS = 30
MATURITY_GROWING_VIDEOS = 75
MATURITY_STABLE_VIDEOS = 150
EMPIRICAL_PRIOR_STRENGTH = 50.0
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_DATA_DIR = SCRIPT_DIR.parent / "data"

# All decisions are relative to the channel's own distribution. Priors prevent
# tiny cohorts from producing extreme conclusions; empirical data gradually
# takes over as the channel accumulates videos.
STRICTNESS_PROFILES = {
    "loose": {
        "label": "느슨함",
        "top_fraction": 0.50,
        "keyword_prior_strength": 1.0,
        "keyword_min_fraction": 0.02,
        "strategy_quantile": 0.45,
        "duplicate_quantile": 0.90,
        "duplicate_prior": 0.55,
        "review_quantile": 0.65,
        "review_prior": 0.30,
    },
    "balanced": {
        "label": "중간",
        "top_fraction": 0.35,
        "keyword_prior_strength": 2.0,
        "keyword_min_fraction": 0.04,
        "strategy_quantile": 0.50,
        "duplicate_quantile": 0.80,
        "duplicate_prior": 0.45,
        "review_quantile": 0.55,
        "review_prior": 0.25,
    },
    "strict": {
        "label": "엄격함",
        "top_fraction": 0.25,
        "keyword_prior_strength": 3.0,
        "keyword_min_fraction": 0.06,
        "strategy_quantile": 0.60,
        "duplicate_quantile": 0.65,
        "duplicate_prior": 0.35,
        "review_quantile": 0.40,
        "review_prior": 0.18,
    },
}
STRICTNESS_ALIASES = {
    "loose": "loose", "relaxed": "loose", "느슨": "loose", "느슨함": "loose",
    "balanced": "balanced", "medium": "balanced", "중간": "balanced", "보통": "balanced",
    "strict": "strict", "엄격": "strict", "엄격함": "strict",
}


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


def normalize_strictness(value: str | None) -> str:
    normalized = STRICTNESS_ALIASES.get(str(value or "balanced").strip().lower())
    if normalized is None:
        allowed = ", ".join(STRICTNESS_PROFILES)
        raise ValueError(f"YOUTUBE_FEEDBACK_STRICTNESS must be one of {allowed}: {value}")
    return normalized


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
    creator_content_type TEXT,
    shorts_feed_views INTEGER,
    shorts_feed_engaged_views INTEGER,
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
CREATE TABLE IF NOT EXISTS objectives (
    objective_id INTEGER PRIMARY KEY AUTOINCREMENT,
    objective_type TEXT NOT NULL,
    profile_version INTEGER NOT NULL DEFAULT 1,
    target_value REAL,
    target_unit TEXT,
    improvement_target REAL,
    horizon_days INTEGER NOT NULL DEFAULT 28,
    priority INTEGER NOT NULL DEFAULT 1,
    weights_json TEXT NOT NULL,
    constraints_json TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS video_jobs (
    job_id TEXT PRIMARY KEY,
    video_id TEXT UNIQUE,
    plan_id INTEGER,
    objective_id INTEGER,
    topic TEXT NOT NULL,
    uploaded_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(video_id) REFERENCES videos(video_id),
    FOREIGN KEY(objective_id) REFERENCES objectives(objective_id)
);
CREATE TABLE IF NOT EXISTS content_features (
    video_id TEXT PRIMARY KEY,
    job_id TEXT,
    objective_type TEXT,
    topic_family TEXT,
    angle TEXT,
    format_type TEXT,
    hook_type TEXT,
    emotion_curve_json TEXT,
    cta_type TEXT,
    main_keyword TEXT,
    sub_keywords_json TEXT,
    series_key TEXT,
    exploration_mode TEXT,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(video_id) REFERENCES videos(video_id),
    FOREIGN KEY(job_id) REFERENCES video_jobs(job_id)
);
CREATE TABLE IF NOT EXISTS performance_snapshots (
    video_id TEXT NOT NULL,
    window_name TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    elapsed_days INTEGER,
    views INTEGER,
    engaged_views INTEGER,
    average_view_duration REAL,
    average_view_percentage REAL,
    likes INTEGER,
    dislikes INTEGER,
    comments INTEGER,
    shares INTEGER,
    subscribers_gained INTEGER,
    subscribers_lost INTEGER,
    shorts_feed_views INTEGER,
    shorts_feed_engaged_views INTEGER,
    search_views INTEGER,
    browse_views INTEGER,
    suggested_views INTEGER,
    external_views INTEGER,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY(video_id, window_name),
    FOREIGN KEY(video_id) REFERENCES videos(video_id)
);
CREATE TABLE IF NOT EXISTS planning_runs (
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    objective_id INTEGER NOT NULL,
    seed_topic TEXT,
    candidate_pool_json TEXT NOT NULL,
    planner_output_json TEXT,
    critic_output_json TEXT,
    selected_candidate_json TEXT,
    selection_mode TEXT NOT NULL,
    base_score REAL,
    adjusted_score REAL,
    confidence REAL,
    decision TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(objective_id) REFERENCES objectives(objective_id)
);
CREATE TABLE IF NOT EXISTS strategy_hypotheses (
    hypothesis_id INTEGER PRIMARY KEY AUTOINCREMENT,
    objective_type TEXT NOT NULL,
    statement TEXT NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    contradiction_refs_json TEXT NOT NULL,
    confounders_json TEXT NOT NULL,
    support_count INTEGER NOT NULL DEFAULT 0,
    contradiction_count INTEGER NOT NULL DEFAULT 0,
    confidence REAL NOT NULL DEFAULT 0.0,
    status TEXT NOT NULL DEFAULT 'testing',
    ttl_videos INTEGER NOT NULL DEFAULT 10,
    videos_since_validation INTEGER NOT NULL DEFAULT 0,
    last_validated_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS hypothesis_observations (
    hypothesis_id INTEGER NOT NULL,
    video_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(hypothesis_id, video_id),
    FOREIGN KEY(hypothesis_id) REFERENCES strategy_hypotheses(hypothesis_id),
    FOREIGN KEY(video_id) REFERENCES videos(video_id)
);
CREATE TABLE IF NOT EXISTS strategy_audits (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    objective_type TEXT NOT NULL,
    audit_type TEXT NOT NULL,
    video_count INTEGER NOT NULL,
    model TEXT,
    output_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trend_observations (
    topic TEXT NOT NULL,
    source TEXT NOT NULL,
    observed_date TEXT NOT NULL,
    PRIMARY KEY(topic, source, observed_date)
);
CREATE TABLE IF NOT EXISTS claude_usage (
    usage_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT,
    plan_id INTEGER,
    stage TEXT NOT NULL,
    attempt INTEGER NOT NULL DEFAULT 1,
    model TEXT NOT NULL,
    response_id TEXT,
    request_id TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_input_tokens INTEGER NOT NULL DEFAULT 0,
    web_search_requests INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_window ON performance_snapshots(window_name, period_end);
CREATE INDEX IF NOT EXISTS idx_planning_runs_job ON planning_runs(job_id);
CREATE INDEX IF NOT EXISTS idx_usage_job ON claude_usage(job_id);
CREATE INDEX IF NOT EXISTS idx_hypotheses_objective ON strategy_hypotheses(objective_type, status);
"""

ANALYTICS_COLUMN_MIGRATIONS = {
    "creator_content_type": "TEXT",
    "shorts_feed_views": "INTEGER",
    "shorts_feed_engaged_views": "INTEGER",
}

VIDEO_COLUMN_MIGRATIONS = {
    "upload_jst_hour": "INTEGER",
    "upload_jst_weekday": "INTEGER",
    "upload_month": "INTEGER",
    "hours_since_previous_upload": "REAL",
}


def connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(analytics)")}
    for column, column_type in ANALYTICS_COLUMN_MIGRATIONS.items():
        if column not in existing_columns:
            conn.execute(f"ALTER TABLE analytics ADD COLUMN {column} {column_type}")
    existing_video_columns = {row["name"] for row in conn.execute("PRAGMA table_info(videos)")}
    for column, column_type in VIDEO_COLUMN_MIGRATIONS.items():
        if column not in existing_video_columns:
            conn.execute(f"ALTER TABLE videos ADD COLUMN {column} {column_type}")
    conn.commit()
    return conn


STOPWORDS = {
    "\uadf8\ub9ac\uace0", "\uadf8\ub7ec\ub098", "\ud558\uc9c0\ub9cc", "\ub610\ub294", "\ub300\ud55c", "\uc704\ud55c", "\ud1b5\ud574", "\uad00\ub828", "\uc774\uac83", "\uc800\uac83",
    "\uc601\uc0c1", "\uc1fc\uce20", "shorts", "youtube", "\uc720\ud29c\ube0c", "\uc815\ub9d0", "\ubc14\ub85c", "\uc624\ub298", "\uc6b0\ub9ac", "\uc5ec\ub7ec\ubd84",
    "\uc788\ub294", "\uc5c6\ub294", "\ud558\ub294", "\ub418\ub294", "\uc785\ub2c8\ub2e4", "\ud569\ub2c8\ub2e4", "\ud558\uc138\uc694", "\uc788\uc2b5\ub2c8\ub2e4", "\uc5c6\uc2b5\ub2c8\ub2e4",
    "\uac00\uc9c0", "\ubc29\ubc95", "\uc774\uc720", "\ub54c\ubb38", "\ub300\ud574", "\uc5d0\uc11c", "\uc73c\ub85c", "\uc5d0\uac8c", "\ubd80\ud130", "\uae4c\uc9c0", "\ucc98\ub7fc",
    "\uc774\ub807\uac8c", "\uc788\uc5c8\uc2b5\ub2c8\ub2e4", "\ub300\uc5d0", "\uc88b\ub2e4", "\uacbd\uc6b0", "\uc0ac\ub78c", "\uc815\ub3c4", "\uc54c\ub824",
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


def topic_verdict(similarity: float, thresholds: dict[str, float] | None = None) -> str:
    thresholds = thresholds or {"duplicate": 0.55, "review": 0.30}
    if similarity >= thresholds["duplicate"]:
        return "\uc911\ubcf5 \uac00\ub2a5\uc131 \ub192\uc74c"
    if similarity >= thresholds["review"]:
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
    client_secret = _env_path("YOUTUBE_FEEDBACK_CLIENT_SECRET_FILE")
    if client_secret is None:
        # Backward compatibility with the original guide. The upload pipeline's
        # raw secret value is deliberately ignored because it is not a file.
        legacy_client_secret = _env_path("YOUTUBE_CLIENT_SECRET")
        if legacy_client_secret is not None and legacy_client_secret.is_file():
            client_secret = legacy_client_secret
    if token is None:
        raise RuntimeError("YOUTUBE_FEEDBACK_TOKEN\uc5d0 \ubcc4\ub3c4 \uc77d\uae30 \ud1a0\ud070 \uacbd\ub85c\ub97c \uc9c0\uc815\ud558\uc138\uc694.")
    creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if client_secret is None or not client_secret.is_file():
            raise RuntimeError(
                "YOUTUBE_FEEDBACK_CLIENT_SECRET_FILE\uc5d0 \ub370\uc2a4\ud06c\ud1b1 OAuth client_secret.json "
                "\uacbd\ub85c\ub97c \uc9c0\uc815\ud558\uc138\uc694."
            )
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
        # creatorContentType is not compatible with the video dimension in the
        # channel report for every metric/account combination. Shorts identity
        # is attached by the separate SHORTS traffic-source query below.
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
    skipped: list[str] = []
    for metric in ANALYTICS_METRICS:
        try:
            response = _analytics_query(analytics, start, end, (metric,))
        except Exception as exc:
            if _http_status(exc) == 400:
                skipped.append(metric)
                continue
            raise
        supported.append(metric)
        _merge_analytics_response(merged, response)
    if skipped:
        print(f"\u26a0\ufe0f  Analytics \uc9c0\ud45c \uc2a4\ud0b5\ub428 (400 \uc751\ub2f5): {', '.join(skipped)}", file=sys.stderr)
    if not supported:
        raise RuntimeError("\ud604\uc7ac \ucc44\ub110/\uae30\uac04\uc5d0\uc11c \uc9c0\uc6d0\ub418\ub294 Analytics \uc9c0\ud45c\ub97c \ucc3e\uc9c0 \ubabb\ud588\uc2b5\ub2c8\ub2e4.")
    return merged, supported


def fetch_shorts_feed_analytics(
    analytics,
    start: str,
    end: str,
    video_ids: Sequence[str],
) -> dict[str, dict[str, Any]]:
    """Collect Shorts-feed traffic separately from the all-traffic video report.

    YouTube does not expose Shorts feed impressions or a stayed-vs-swiped metric
    through this API. The official SHORTS traffic-source rows still let us
    measure feed-attributed starts and engaged views without mixing long-form CTR.
    """
    result: dict[str, dict[str, Any]] = {}
    for group in chunks(video_ids, 10):
        parameters = {
            "ids": "channel==MINE",
            "startDate": start,
            "endDate": end,
            "metrics": "views,engagedViews",
            "dimensions": "video,insightTrafficSourceType",
            "filters": f"video=={','.join(group)}",
            "maxResults": 200,
        }
        response = analytics.reports().query(**parameters).execute()
        headers = [header.get("name") for header in response.get("columnHeaders") or []]
        for values in response.get("rows") or []:
            row = dict(zip(headers, values))
            if row.get("insightTrafficSourceType") != "SHORTS" or not row.get("video"):
                continue
            result[str(row["video"])] = {
                "creatorContentType": "SHORTS",
                "shortsFeedViews": row.get("views"),
                "shortsFeedEngagedViews": row.get("engagedViews"),
            }
    return result


VIDEO_UPSERT = """
INSERT INTO videos (
    video_id, title, description, published_at, duration_seconds,
    view_count, like_count, comment_count, fetched_at, upload_jst_hour,
    upload_jst_weekday, upload_month, hours_since_previous_upload
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(video_id) DO UPDATE SET
    title=excluded.title,
    description=excluded.description,
    published_at=excluded.published_at,
    duration_seconds=COALESCE(excluded.duration_seconds, videos.duration_seconds),
    view_count=COALESCE(excluded.view_count, videos.view_count),
    like_count=COALESCE(excluded.like_count, videos.like_count),
    comment_count=COALESCE(excluded.comment_count, videos.comment_count),
    upload_jst_hour=COALESCE(excluded.upload_jst_hour, videos.upload_jst_hour),
    upload_jst_weekday=COALESCE(excluded.upload_jst_weekday, videos.upload_jst_weekday),
    upload_month=COALESCE(excluded.upload_month, videos.upload_month),
    hours_since_previous_upload=COALESCE(excluded.hours_since_previous_upload, videos.hours_since_previous_upload),
    fetched_at=excluded.fetched_at
"""

ANALYTICS_UPSERT = """
INSERT INTO analytics (
    video_id, period_start, period_end, views, engaged_views,
    average_view_duration, average_view_percentage, likes, dislikes,
    comments, shares, subscribers_gained, subscribers_lost,
    creator_content_type, shorts_feed_views, shorts_feed_engaged_views,
    performance_score, fetched_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
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
    creator_content_type=COALESCE(excluded.creator_content_type, analytics.creator_content_type),
    shorts_feed_views=COALESCE(excluded.shorts_feed_views, analytics.shorts_feed_views),
    shorts_feed_engaged_views=COALESCE(excluded.shorts_feed_engaged_views, analytics.shorts_feed_engaged_views),
    fetched_at=excluded.fetched_at
"""


def store_videos(conn: sqlite3.Connection, videos: Sequence[dict[str, Any]]) -> None:
    def parsed(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    dated = sorted(
        ((parsed(video.get("published_at")), video.get("video_id")) for video in videos),
        key=lambda item: item[0] or datetime.min.replace(tzinfo=timezone.utc),
    )
    previous_hours: dict[str, float | None] = {}
    previous: datetime | None = None
    for published, video_id in dated:
        if not video_id:
            continue
        previous_hours[str(video_id)] = (
            (published - previous).total_seconds() / 3600.0
            if published is not None and previous is not None else None
        )
        if published is not None:
            previous = published

    jst = timezone(timedelta(hours=9))
    for video in videos:
        published = parsed(video.get("published_at"))
        published_jst = published.astimezone(jst) if published else None
        conn.execute(VIDEO_UPSERT, (
            video["video_id"], video["title"], video.get("description"), video.get("published_at"),
            video.get("duration_seconds"), video.get("view_count"), video.get("like_count"),
            video.get("comment_count"), video["fetched_at"],
            published_jst.hour if published_jst else None,
            published_jst.weekday() if published_jst else None,
            published_jst.month if published_jst else None,
            previous_hours.get(str(video["video_id"])),
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
            values["subscribers_lost"], source.get("creatorContentType"),
            nullable_number(source.get("shortsFeedViews")),
            nullable_number(source.get("shortsFeedEngagedViews")), fetched_at,
        ))


SNAPSHOT_WINDOWS = ("D1_APPROX", "D7", "D28", "ROLLING_90D")
SNAPSHOT_UPSERT = """
INSERT INTO performance_snapshots (
    video_id, window_name, period_start, period_end, elapsed_days, views,
    engaged_views, average_view_duration, average_view_percentage, likes,
    dislikes, comments, shares, subscribers_gained, subscribers_lost,
    shorts_feed_views, shorts_feed_engaged_views, search_views, browse_views,
    suggested_views, external_views, fetched_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(video_id, window_name) DO UPDATE SET
    period_start=excluded.period_start,
    period_end=excluded.period_end,
    elapsed_days=excluded.elapsed_days,
    views=excluded.views,
    engaged_views=excluded.engaged_views,
    average_view_duration=excluded.average_view_duration,
    average_view_percentage=excluded.average_view_percentage,
    likes=excluded.likes,
    dislikes=excluded.dislikes,
    comments=excluded.comments,
    shares=excluded.shares,
    subscribers_gained=excluded.subscribers_gained,
    subscribers_lost=excluded.subscribers_lost,
    shorts_feed_views=excluded.shorts_feed_views,
    shorts_feed_engaged_views=excluded.shorts_feed_engaged_views,
    search_views=excluded.search_views,
    browse_views=excluded.browse_views,
    suggested_views=excluded.suggested_views,
    external_views=excluded.external_views,
    fetched_at=excluded.fetched_at
"""


def duration_bucket(seconds: int | float | None) -> str:
    value = float(seconds or 0)
    if value <= 35:
        return "0_35"
    if value <= 50:
        return "36_50"
    if value <= 65:
        return "51_65"
    return "66_plus"


def snapshot_period(
    published_at: str,
    window_name: str,
    *,
    as_of: date | None = None,
) -> tuple[str, str, int] | None:
    if window_name not in SNAPSHOT_WINDOWS:
        raise ValueError(f"알 수 없는 snapshot window: {window_name}")
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00")).date()
    except (AttributeError, ValueError):
        return None
    available_end = as_of or (date.today() - timedelta(days=2))
    elapsed = max(0, (available_end - published).days + 1)
    if window_name == "D1_APPROX":
        required_days = 2
    elif window_name == "D7":
        required_days = 7
    elif window_name == "D28":
        required_days = 28
    else:
        start = max(published, available_end - timedelta(days=89))
        return start.isoformat(), available_end.isoformat(), elapsed
    if elapsed < required_days:
        return None
    end = published + timedelta(days=required_days - 1)
    return published.isoformat(), end.isoformat(), elapsed


def store_performance_snapshot(conn: sqlite3.Connection, snapshot: dict[str, Any]) -> None:
    window_name = str(snapshot.get("window_name") or "")
    if window_name not in SNAPSHOT_WINDOWS:
        raise ValueError(f"알 수 없는 snapshot window: {window_name}")
    conn.execute(SNAPSHOT_UPSERT, (
        snapshot["video_id"], window_name, snapshot["period_start"], snapshot["period_end"],
        snapshot.get("elapsed_days"), nullable_number(snapshot.get("views")),
        nullable_number(snapshot.get("engaged_views")),
        nullable_number(snapshot.get("average_view_duration"), float),
        nullable_number(snapshot.get("average_view_percentage"), float),
        nullable_number(snapshot.get("likes")), nullable_number(snapshot.get("dislikes")),
        nullable_number(snapshot.get("comments")), nullable_number(snapshot.get("shares")),
        nullable_number(snapshot.get("subscribers_gained")),
        nullable_number(snapshot.get("subscribers_lost")),
        nullable_number(snapshot.get("shorts_feed_views")),
        nullable_number(snapshot.get("shorts_feed_engaged_views")),
        nullable_number(snapshot.get("search_views")), nullable_number(snapshot.get("browse_views")),
        nullable_number(snapshot.get("suggested_views")), nullable_number(snapshot.get("external_views")),
        snapshot.get("fetched_at") or iso_now(),
    ))


def store_performance_snapshots(
    conn: sqlite3.Connection,
    snapshots: Sequence[dict[str, Any]],
) -> None:
    for snapshot in snapshots:
        store_performance_snapshot(conn, snapshot)


def _snapshot_from_api_row(
    video_id: str,
    window_name: str,
    period_start: str,
    period_end: str,
    elapsed_days: int,
    source: dict[str, Any],
    fetched_at: str,
) -> dict[str, Any]:
    return {
        "video_id": video_id,
        "window_name": window_name,
        "period_start": period_start,
        "period_end": period_end,
        "elapsed_days": elapsed_days,
        **{column: source.get(metric) for metric, column in METRIC_COLUMNS.items()},
        "shorts_feed_views": source.get("shortsFeedViews"),
        "shorts_feed_engaged_views": source.get("shortsFeedEngagedViews"),
        "search_views": source.get("searchViews"),
        "browse_views": source.get("browseViews"),
        "suggested_views": source.get("suggestedViews"),
        "external_views": source.get("externalViews"),
        "fetched_at": fetched_at,
    }


def copy_rolling_snapshots(
    conn: sqlite3.Connection,
    rows: dict[str, dict[str, Any]],
    period_start: str,
    period_end: str,
    fetched_at: str,
) -> None:
    for video_id, source in rows.items():
        try:
            elapsed = (date.fromisoformat(period_end) - date.fromisoformat(period_start)).days + 1
        except ValueError:
            elapsed = None
        store_performance_snapshot(conn, _snapshot_from_api_row(
            video_id, "ROLLING_90D", period_start, period_end, elapsed or 90, source, fetched_at
        ))


def _fetch_video_window_metrics(analytics, video_id: str, start: str, end: str) -> dict[str, Any]:
    merged: dict[str, dict[str, Any]] = {}
    parameters = {
        "ids": "channel==MINE", "startDate": start, "endDate": end,
        "metrics": ",".join(ANALYTICS_METRICS), "dimensions": "video",
        "filters": f"video=={video_id}", "maxResults": 1,
    }
    try:
        _merge_analytics_response(merged, analytics.reports().query(**parameters).execute())
    except Exception as exc:
        if _http_status(exc) != 400:
            raise
        for metric in ANALYTICS_METRICS:
            one = dict(parameters)
            one["metrics"] = metric
            try:
                _merge_analytics_response(merged, analytics.reports().query(**one).execute())
            except Exception as metric_exc:
                if _http_status(metric_exc) != 400:
                    raise
    source = merged.get(video_id, {})
    try:
        feed = fetch_shorts_feed_analytics(analytics, start, end, [video_id]).get(video_id, {})
        source.update(feed)
    except Exception:
        pass
    try:
        traffic = analytics.reports().query(
            ids="channel==MINE", startDate=start, endDate=end, metrics="views",
            dimensions="insightTrafficSourceType", filters=f"video=={video_id}", maxResults=200,
        ).execute()
        headers = [item.get("name") for item in traffic.get("columnHeaders") or []]
        keys = {
            "YT_SEARCH": "searchViews", "SUBSCRIBER": "browseViews",
            "RELATED_VIDEO": "suggestedViews", "EXT_URL": "externalViews",
        }
        for values in traffic.get("rows") or []:
            row = dict(zip(headers, values))
            destination = keys.get(str(row.get("insightTrafficSourceType")))
            if destination:
                source[destination] = int(source.get(destination) or 0) + int(row.get("views") or 0)
    except Exception:
        pass
    return source


def update_mature_snapshots(
    conn: sqlite3.Connection,
    analytics,
    videos: Sequence[dict[str, Any]],
    *,
    as_of: date | None = None,
    fetched_at: str | None = None,
) -> int:
    stored = 0
    fetched_at = fetched_at or iso_now()
    for video in videos:
        for window_name in ("D1_APPROX", "D7", "D28"):
            period = snapshot_period(video.get("published_at"), window_name, as_of=as_of)
            if period is None:
                continue
            start, end, elapsed = period
            try:
                source = _fetch_video_window_metrics(analytics, video["video_id"], start, end)
            except Exception as exc:
                print(
                    f"snapshot {window_name} 수집 실패({video['video_id']}, 계속): {type(exc).__name__}",
                    file=sys.stderr,
                )
                continue
            if not source:
                continue
            store_performance_snapshot(conn, _snapshot_from_api_row(
                video["video_id"], window_name, start, end, elapsed, source, fetched_at
            ))
            stored += 1
    return stored


def _ratio(numerator: float | int | None, denominator: float | int | None) -> float | None:
    if numerator is None or denominator is None or float(denominator) <= 0:
        return None
    return float(numerator) / float(denominator)


def derive_video_metrics(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    views = row["views"]
    engaged_views = row["engaged_views"]
    feed_views = row["shorts_feed_views"]
    feed_engaged_views = row["shorts_feed_engaged_views"]
    if feed_views is not None and feed_views > 0 and feed_engaged_views is not None:
        initial_engagement_rate = _ratio(feed_engaged_views, feed_views)
        initial_engagement_source = "shorts_feed"
    else:
        initial_engagement_rate = _ratio(engaged_views, views)
        initial_engagement_source = "all_views_proxy"

    subscribers_gained = row["subscribers_gained"]
    subscribers_lost = row["subscribers_lost"]
    net_subscribers = None
    if subscribers_gained is not None or subscribers_lost is not None:
        net_subscribers = (subscribers_gained or 0) - (subscribers_lost or 0)
    average_percentage = nullable_number(row["average_view_percentage"], float)
    return {
        "initial_engagement_rate": initial_engagement_rate,
        "initial_engagement_source": initial_engagement_source,
        "shorts_feed_view_share": _ratio(feed_views, views),
        "average_view_duration": nullable_number(row["average_view_duration"], float),
        "average_view_percentage": average_percentage,
        "subscriber_conversion_rate": _ratio(subscribers_gained, views),
        "net_subscriber_conversion_rate": _ratio(net_subscribers, views),
        "like_rate": _ratio(row["analytics_likes"] if "analytics_likes" in row.keys() else row["likes"], views),
        "comment_rate": _ratio(row["analytics_comments"] if "analytics_comments" in row.keys() else row["comments"], views),
        "share_rate": _ratio(row["shares"], views),
        "replay_lift": max(float(average_percentage or 0) - 100.0, 0.0),
    }


def quantile(values: Sequence[float | int], fraction: float) -> float | None:
    """Return an interpolated quantile without requiring numpy."""
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    position = max(0.0, min(1.0, fraction)) * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def percentile_rank(value: float, cohort: Sequence[float]) -> float:
    """Mid-rank percentile. Equal values remain neutral instead of winning ties."""
    if not cohort:
        return 0.5
    lower = sum(item < value for item in cohort)
    equal = sum(item == value for item in cohort)
    return (lower + 0.5 * equal) / len(cohort)


def confidence_level(scored_count: int) -> str:
    if scored_count < MATURITY_LEARNING_VIDEOS:
        return "low"
    if scored_count < MATURITY_GROWING_VIDEOS:
        return "medium"
    if scored_count < MATURITY_STABLE_VIDEOS:
        return "growing"
    return "high"


def cohort_reliability(scored_count: int, prior_strength: float = EMPIRICAL_PRIOR_STRENGTH) -> float:
    return max(0.0, min(1.0, float(scored_count) / (float(scored_count) + prior_strength)))


def shrink_percentile(
    percentile: float,
    *,
    cohort_size: int,
    sample: float,
    cohort_median_sample: float,
) -> tuple[float, float]:
    cohort_confidence = cohort_reliability(cohort_size)
    sample_confidence = min(1.0, math.sqrt(max(0.0, sample) / max(1.0, cohort_median_sample)))
    reliability = cohort_confidence * sample_confidence
    adjusted = 0.5 + (float(percentile) - 0.5) * reliability
    return adjusted, reliability


def _snapshot_derived(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    views = row["views"]
    feed_views = row["shorts_feed_views"]
    feed_engaged = row["shorts_feed_engaged_views"]
    engaged = row["engaged_views"]
    initial = _ratio(feed_engaged, feed_views) if feed_views else _ratio(engaged, views)
    gained = row["subscribers_gained"]
    lost = row["subscribers_lost"]
    net = None if gained is None and lost is None else (gained or 0) - (lost or 0)
    average_percentage = nullable_number(row["average_view_percentage"], float)
    return {
        "views_percentile": nullable_number(views, float),
        "initial_engagement": initial,
        "average_view_percentage": average_percentage,
        "subscriber_conversion": _ratio(gained, views),
        "net_subscriber_conversion": _ratio(net, views),
        "share_rate": _ratio(row["shares"], views),
        "like_rate": _ratio(row["likes"], views),
        "comment_rate": _ratio(row["comments"], views),
        "replay_lift": max(float(average_percentage or 0.0) - 100.0, 0.0),
        "shorts_feed_view_share": _ratio(feed_views, views),
    }


def snapshot_cohort(
    conn: sqlite3.Connection,
    video_id: str,
    window_name: str = "D28",
) -> list[sqlite3.Row]:
    target = conn.execute(
        """SELECT s.*, v.duration_seconds, f.topic_family
           FROM performance_snapshots s
           JOIN videos v ON v.video_id=s.video_id
           LEFT JOIN content_features f ON f.video_id=s.video_id
           WHERE s.video_id=? AND s.window_name=?
             AND NOT EXISTS (
               SELECT 1 FROM analytics a WHERE a.video_id=s.video_id
                 AND a.creator_content_type IS NOT NULL AND a.creator_content_type!='SHORTS'
             )""",
        (video_id, window_name),
    ).fetchone()
    if target is None:
        return []
    bucket = duration_bucket(target["duration_seconds"])
    all_rows = conn.execute(
        """SELECT s.*, v.duration_seconds, f.topic_family
           FROM performance_snapshots s
           JOIN videos v ON v.video_id=s.video_id
           LEFT JOIN content_features f ON f.video_id=s.video_id
           WHERE s.window_name=?
             AND NOT EXISTS (
               SELECT 1 FROM analytics a WHERE a.video_id=s.video_id
                 AND a.creator_content_type IS NOT NULL AND a.creator_content_type!='SHORTS'
             )""",
        (window_name,),
    ).fetchall()
    duration_rows = [row for row in all_rows if duration_bucket(row["duration_seconds"]) == bucket]
    family = target["topic_family"]
    family_rows = [row for row in duration_rows if family and row["topic_family"] == family]
    if len(family_rows) >= 5:
        return family_rows
    return duration_rows or all_rows


def normalized_snapshot_metrics(
    conn: sqlite3.Connection,
    video_id: str,
    window_name: str = "D28",
) -> dict[str, Any]:
    cohort = snapshot_cohort(conn, video_id, window_name)
    target = next((row for row in cohort if row["video_id"] == video_id), None)
    if target is None:
        return {"video_id": video_id, "window_name": window_name, "confidence": 0.0,
                "confidence_level": "low", "metrics": {}}
    derived_rows = [(row, _snapshot_derived(row)) for row in cohort]
    samples = [max(float(row["views"] or 0), float(row["engaged_views"] or 0), 1.0) for row, _ in derived_rows]
    median_sample = statistics.median(samples) if samples else 1.0
    target_sample = max(float(target["views"] or 0), float(target["engaged_views"] or 0), 1.0)
    output: dict[str, float] = {}
    reliabilities = []
    target_metrics = _snapshot_derived(target)
    for metric, value in target_metrics.items():
        values = [float(item[metric]) for _, item in derived_rows if item.get(metric) is not None]
        if value is None or not values:
            output[metric] = 0.5
            continue
        percentile = percentile_rank(float(value), values)
        adjusted, reliability = shrink_percentile(
            percentile, cohort_size=len(cohort), sample=target_sample,
            cohort_median_sample=median_sample,
        )
        output[metric] = round(adjusted, 6)
        reliabilities.append(reliability)
    # These are derived aliases used by objective profiles.
    output["duration_matched_retention"] = output.get("average_view_percentage", 0.5)
    output["topic_family_conversion"] = output.get("net_subscriber_conversion", 0.5)
    return {
        "video_id": video_id,
        "window_name": window_name,
        "cohort_size": len(cohort),
        "duration_bucket": duration_bucket(target["duration_seconds"]),
        "confidence": round(min(reliabilities) if reliabilities else 0.0, 6),
        "confidence_level": confidence_level(len(cohort)),
        "metrics": output,
    }


def classify_exposure_quality(
    conn: sqlite3.Connection,
    video_id: str,
    window_name: str = "D28",
) -> dict[str, Any]:
    normalized = normalized_snapshot_metrics(conn, video_id, window_name)
    metrics = normalized["metrics"]
    if not metrics or normalized.get("cohort_size", 0) < 3:
        return {**normalized, "classification": "insufficient_data", "quality_score": None}
    quality_names = (
        "initial_engagement", "average_view_percentage", "share_rate",
        "net_subscriber_conversion",
    )
    quality = statistics.mean(metrics.get(name, 0.5) for name in quality_names)
    views = metrics.get("views_percentile", 0.5)
    if views >= 0.70 and quality >= 0.70:
        classification = "strategy_success"
    elif views >= 0.70 and quality < 0.70:
        classification = "exposure_luck"
    elif views < 0.50 and quality >= 0.70:
        classification = "hidden_success"
    else:
        classification = "weak_result"
    return {**normalized, "classification": classification, "quality_score": round(quality, 6)}


classify_video_outcome = classify_exposure_quality


def register_video_job(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    topic: str,
    plan_id: int | None = None,
    objective_id: int | None = None,
) -> None:
    conn.execute(
        """INSERT INTO video_jobs (job_id, plan_id, objective_id, topic, created_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(job_id) DO UPDATE SET
             plan_id=COALESCE(excluded.plan_id, video_jobs.plan_id),
             objective_id=COALESCE(excluded.objective_id, video_jobs.objective_id),
             topic=excluded.topic""",
        (job_id, plan_id, objective_id, topic, iso_now()),
    )


def link_uploaded_video(
    conn: sqlite3.Connection,
    *,
    job_id: str,
    video_id: str,
    uploaded_at: str,
    topic: str = "",
    plan: dict[str, Any] | None = None,
) -> None:
    plan = plan or {}
    existing_job = conn.execute("SELECT * FROM video_jobs WHERE job_id=?", (job_id,)).fetchone()
    resolved_topic = topic or (existing_job["topic"] if existing_job else "") or plan.get("topic") or "(미확인 주제)"
    conn.execute(
        """INSERT INTO videos (video_id, title, description, published_at, fetched_at)
           VALUES (?, ?, '', ?, ?)
           ON CONFLICT(video_id) DO UPDATE SET published_at=COALESCE(videos.published_at, excluded.published_at)""",
        (video_id, resolved_topic, uploaded_at, iso_now()),
    )
    if existing_job is None:
        register_video_job(
            conn, job_id=job_id, topic=resolved_topic,
            plan_id=((plan.get("objective") or {}).get("plan_id")),
            objective_id=((plan.get("objective") or {}).get("objective_id")),
        )
    conn.execute(
        "UPDATE video_jobs SET video_id=?, uploaded_at=?, topic=? WHERE job_id=?",
        (video_id, uploaded_at, resolved_topic, job_id),
    )
    design = plan.get("content_design") or {}
    objective = plan.get("objective") or {}
    if design or objective:
        conn.execute(
            """INSERT INTO content_features (
                video_id, job_id, objective_type, topic_family, angle, format_type,
                hook_type, emotion_curve_json, cta_type, main_keyword,
                sub_keywords_json, series_key, exploration_mode, plan_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(video_id) DO UPDATE SET
                job_id=excluded.job_id, objective_type=excluded.objective_type,
                topic_family=excluded.topic_family, angle=excluded.angle,
                format_type=excluded.format_type, hook_type=excluded.hook_type,
                emotion_curve_json=excluded.emotion_curve_json,
                cta_type=excluded.cta_type, main_keyword=excluded.main_keyword,
                sub_keywords_json=excluded.sub_keywords_json, series_key=excluded.series_key,
                exploration_mode=excluded.exploration_mode, plan_json=excluded.plan_json""",
            (
                video_id, job_id, objective.get("type"), design.get("topic_family"),
                design.get("angle"), design.get("format_type"), plan.get("hook_type"),
                json.dumps(design.get("emotion_curve") or [], ensure_ascii=False),
                design.get("cta_type"), plan.get("main_keyword"),
                json.dumps(plan.get("sub_keywords") or [], ensure_ascii=False),
                design.get("series_key"), objective.get("selection_mode"),
                json.dumps(plan, ensure_ascii=False), iso_now(),
            ),
        )


def hypothesis_confidence(support_count: int, contradiction_count: int) -> float:
    total = max(0, int(support_count)) + max(0, int(contradiction_count))
    raw = max(0, int(support_count)) / max(1, total)
    sample_factor = total / (total + 6.0)
    return 0.5 + (raw - 0.5) * sample_factor


def advance_hypothesis_ttl(conn: sqlite3.Connection, objective_type: str | None = None) -> int:
    where = " AND objective_type=?" if objective_type else ""
    values = (objective_type,) if objective_type else ()
    rows = conn.execute(
        "SELECT hypothesis_id, ttl_videos, videos_since_validation FROM strategy_hypotheses "
        "WHERE status IN ('testing', 'active', 'weakened')" + where,
        values,
    ).fetchall()
    now = iso_now()
    for row in rows:
        since = int(row["videos_since_validation"] or 0) + 1
        status = "expired" if since >= int(row["ttl_videos"] or 10) else None
        conn.execute(
            "UPDATE strategy_hypotheses SET videos_since_validation=?, status=COALESCE(?, status), updated_at=? WHERE hypothesis_id=?",
            (since, status, now, row["hypothesis_id"]),
        )
    return len(rows)


def refresh_hypothesis_evidence(conn: sqlite3.Connection) -> int:
    """Attach newly matured D28 outcomes to matching objective hypotheses once."""
    hypotheses = conn.execute(
        "SELECT * FROM strategy_hypotheses WHERE status IN ('testing','active','weakened')"
    ).fetchall()
    changed = 0
    for hypothesis in hypotheses:
        videos = conn.execute("""
            SELECT j.video_id
            FROM video_jobs j
            JOIN performance_snapshots s ON s.video_id=j.video_id AND s.window_name='D28'
            LEFT JOIN hypothesis_observations o
              ON o.hypothesis_id=? AND o.video_id=j.video_id
            WHERE j.objective_id IN (
                SELECT objective_id FROM objectives WHERE objective_type=?
            ) AND o.video_id IS NULL
        """, (hypothesis["hypothesis_id"], hypothesis["objective_type"])).fetchall()
        evidence_refs = json.loads(hypothesis["evidence_refs_json"] or "[]")
        contradiction_refs = json.loads(hypothesis["contradiction_refs_json"] or "[]")
        support = int(hypothesis["support_count"] or 0)
        contradiction = int(hypothesis["contradiction_count"] or 0)
        for video in videos:
            outcome = classify_exposure_quality(conn, video["video_id"], "D28")["classification"]
            if outcome in {"strategy_success", "hidden_success"}:
                support += 1
                evidence_refs.append(f"video:{video['video_id']}")
                observation = "support"
            else:
                contradiction += 1
                contradiction_refs.append(f"video:{video['video_id']}")
                observation = "contradiction"
            conn.execute(
                "INSERT OR IGNORE INTO hypothesis_observations (hypothesis_id, video_id, outcome, created_at) VALUES (?, ?, ?, ?)",
                (hypothesis["hypothesis_id"], video["video_id"], observation, iso_now()),
            )
            changed += 1
        if not videos:
            continue
        confidence = hypothesis_confidence(support, contradiction)
        if confidence >= 0.60 and support >= 3:
            status = "active"
        elif confidence < 0.45 and contradiction >= 3:
            status = "rejected"
        elif contradiction > support:
            status = "weakened"
        else:
            status = "testing"
        conn.execute("""
            UPDATE strategy_hypotheses
            SET evidence_refs_json=?, contradiction_refs_json=?, support_count=?,
                contradiction_count=?, confidence=?, status=?,
                videos_since_validation=0, last_validated_at=?, updated_at=?
            WHERE hypothesis_id=?
        """, (
            json.dumps(sorted(set(evidence_refs)), ensure_ascii=False),
            json.dumps(sorted(set(contradiction_refs)), ensure_ascii=False),
            support, contradiction, confidence, status, iso_now(), iso_now(),
            hypothesis["hypothesis_id"],
        ))
    return changed


def rebuild_objective_baselines(conn: sqlite3.Connection) -> dict[str, Any]:
    """Recompute dynamic cohort summaries and fill missing topic families."""
    features = conn.execute("""
        SELECT f.video_id, f.topic_family, v.title FROM content_features f
        JOIN videos v ON v.video_id=f.video_id
    """).fetchall()
    family_updates = 0
    for row in features:
        if row["topic_family"]:
            continue
        words = sorted(normalize_keywords(row["title"] or ""))
        family = words[0] if words else "기타"
        conn.execute(
            "UPDATE content_features SET topic_family=? WHERE video_id=?",
            (family, row["video_id"]),
        )
        family_updates += 1
    rows = conn.execute(
        "SELECT * FROM performance_snapshots WHERE window_name='D28'"
    ).fetchall()
    metric_names = (
        "views", "average_view_percentage", "shorts_feed_views",
        "shorts_feed_engaged_views", "shares", "subscribers_gained",
    )
    medians = {}
    for name in metric_names:
        values = [float(row[name]) for row in rows if row[name] is not None]
        medians[name] = statistics.median(values) if values else None
    time_distribution = [dict(row) for row in conn.execute("""
        SELECT upload_jst_hour, COUNT(*) AS video_count
        FROM videos WHERE upload_jst_hour IS NOT NULL
        GROUP BY upload_jst_hour ORDER BY upload_jst_hour
    """)]
    return {
        "snapshot_window": "D28", "video_count": len(rows),
        "cohort_medians": medians, "topic_family_updates": family_updates,
        "upload_time_distribution": time_distribution,
    }


QUADRANT_STRATEGIES = {
    "Q1": {
        "label": "치트키 주제",
        "instruction": "핵심 소재를 시리즈화하되 같은 제목·결론을 복제하지 말 것",
    },
    "Q2": {
        "label": "소재 우수형",
        "instruction": "주제는 유지하고 초반 후킹·전개 속도·편집 밀도를 보완할 것",
    },
    "Q3": {
        "label": "몰입 우수형",
        "instruction": "후반 CTA와 다음 편 예고로 구독할 이유를 강화할 것",
    },
    "Q4": {
        "label": "재검토형",
        "instruction": "작은 실험으로만 재검증하고 제목·주제 각도를 전면 복제하지 말 것",
    },
    "NA": {
        "label": "데이터 부족",
        "instruction": "성과 결론을 내리지 말고 표본이 쌓일 때까지 탐색적으로 사용할 것",
    },
}


def adaptive_strategy_thresholds(
    rows: Sequence[sqlite3.Row | dict[str, Any]],
    strictness: str = "balanced",
) -> dict[str, Any]:
    strictness = normalize_strictness(strictness)
    target_quantile = float(STRICTNESS_PROFILES[strictness]["strategy_quantile"])
    derived_rows = [derive_video_metrics(row) for row in rows]
    retention_values = [
        float(item["average_view_percentage"])
        for item in derived_rows
        if item["average_view_percentage"] is not None
    ]
    # Zero-conversion videos are kept on the low side of the matrix. Using only
    # positive observations for the high threshold avoids classifying 0 >= 0 as high.
    conversion_values = [
        float(item["subscriber_conversion_rate"])
        for item in derived_rows
        if item["subscriber_conversion_rate"] is not None
        and item["subscriber_conversion_rate"] > 0
    ]

    def blended(values: Sequence[float], prior: float) -> tuple[float, float]:
        if not values:
            return prior, 0.0
        empirical = float(quantile(values, target_quantile) or prior)
        empirical_weight = cohort_reliability(len(values))
        return prior * (1.0 - empirical_weight) + empirical * empirical_weight, empirical_weight

    retention, retention_weight = blended(retention_values, 85.0)
    conversion, conversion_weight = blended(conversion_values, 0.005)
    return {
        "retention_percentage": round(retention, 4),
        "subscriber_conversion_rate": round(conversion, 6),
        "target_quantile": target_quantile,
        "retention_sample_count": len(retention_values),
        "conversion_sample_count": len(conversion_values),
        "retention_empirical_weight": round(retention_weight, 4),
        "conversion_empirical_weight": round(conversion_weight, 4),
    }


def classify_video_strategy(
    derived: dict[str, Any],
    thresholds: dict[str, Any],
) -> dict[str, str]:
    retention = derived.get("average_view_percentage")
    conversion = derived.get("subscriber_conversion_rate")
    if retention is None or conversion is None:
        quadrant = "NA"
    else:
        retention_high = float(retention) >= float(thresholds["retention_percentage"])
        conversion_high = (
            float(conversion) > 0
            and float(conversion) >= float(thresholds["subscriber_conversion_rate"])
        )
        if retention_high and conversion_high:
            quadrant = "Q1"
        elif not retention_high and conversion_high:
            quadrant = "Q2"
        elif retention_high and not conversion_high:
            quadrant = "Q3"
        else:
            quadrant = "Q4"
    return {"quadrant": quadrant, **QUADRANT_STRATEGIES[quadrant]}


def calculate_performance_scores(conn: sqlite3.Connection, period_start: str, period_end: str) -> None:
    rows = conn.execute(
        "SELECT * FROM analytics WHERE period_start=? AND period_end=?",
        (period_start, period_end),
    ).fetchall()
    if not rows:
        return

    calculated: list[tuple[str, float, dict[str, float | None]]] = []
    eligible_video_ids = {
        row["video_id"] for row in rows if row["creator_content_type"] in (None, "SHORTS")
    }
    conn.execute(
        "UPDATE analytics SET performance_score=NULL WHERE period_start=? AND period_end=?",
        (period_start, period_end),
    )
    for row in rows:
        if row["video_id"] not in eligible_video_ids:
            continue
        derived = derive_video_metrics(row)
        sample = max(row["shorts_feed_views"] or 0, row["engaged_views"] or 0, row["views"] or 0, 1)
        calculated.append((row["video_id"], float(sample), {
            "shorts_feed_engagement_rate": derived["initial_engagement_rate"],
            "average_view_percentage": derived["average_view_percentage"],
            "share_rate": derived["share_rate"],
            "like_rate": derived["like_rate"],
            "subscriber_rate": derived["net_subscriber_conversion_rate"],
            "comment_rate": derived["comment_rate"],
        }))

    if not calculated:
        return

    cohorts = {
        metric: [float(metrics[metric]) for _, _, metrics in calculated if metrics[metric] is not None]
        for metric in PERFORMANCE_WEIGHTS
    }
    sample_median = statistics.median(sample for _, sample, _ in calculated)
    cohort_confidence = len(calculated) / (len(calculated) + 4.0)

    for video_id, sample, metrics in calculated:
        components = [
            (PERFORMANCE_WEIGHTS[metric], percentile_rank(float(value), cohorts[metric]))
            for metric, value in metrics.items()
            if value is not None and cohorts[metric]
        ]
        total_weight = sum(weight for weight, _ in components)
        if total_weight:
            raw_score = sum(weight * relative for weight, relative in components) / total_weight
            sample_confidence = min(1.0, math.sqrt(sample / max(float(sample_median), 1.0)))
            reliability = cohort_confidence * sample_confidence
            score = 0.5 + (raw_score - 0.5) * reliability
        else:
            score = None
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
        shorts_feed_rows: dict[str, dict[str, Any]] = {}
        try:
            shorts_feed_rows = fetch_shorts_feed_analytics(
                analytics, start_date.isoformat(), end_date.isoformat(), video_ids
            )
        except Exception as exc:
            print(
                f"\uc1fc\uce20 \ud53c\ub4dc \uc720\uc785 \uc9c0\ud45c \uc218\uc9d1 \uc2e4\ud328(\uae30\ubcf8 \uc9c0\ud45c\ub85c \uacc4\uc18d): "
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
        for video_id, feed_values in shorts_feed_rows.items():
            analytics_rows.setdefault(video_id, {}).update(feed_values)

        with conn:
            store_videos(conn, videos)
            store_analytics(conn, analytics_rows, start_date.isoformat(), end_date.isoformat(), fetched_at)
            copy_rolling_snapshots(
                conn, analytics_rows, start_date.isoformat(), end_date.isoformat(), fetched_at
            )
            snapshot_count = update_mature_snapshots(
                conn, analytics, videos, as_of=end_date, fetched_at=fetched_at
            )
            calculate_performance_scores(conn, start_date.isoformat(), end_date.isoformat())
            hypothesis_updates = refresh_hypothesis_evidence(conn)
            conn.execute(
                "UPDATE sync_runs SET finished_at=?, status='success', video_count=?, error_message=NULL WHERE run_id=?",
                (iso_now(), len(videos), run_id),
            )
        print(f"\ucc44\ub110: {channel['title']} ({channel['channel_id']})")
        print(f"\ub3d9\uae30\ud654 \uc644\ub8cc: \uc601\uc0c1 {len(videos)}\uac1c, Analytics \uc601\uc0c1 {len(analytics_rows)}\uac1c")
        print(f"\uc9c0\uc6d0 Analytics \uc9c0\ud45c: {', '.join(supported_metrics)}")
        print(f"\uc1fc\uce20 \ud53c\ub4dc \uc720\uc785 \uc601\uc0c1: {len(shorts_feed_rows)}\uac1c")
        print(f"성과 스냅샷 갱신: {snapshot_count}개 + ROLLING_90D")
        print(f"전략 가설 근거 갱신: {hypothesis_updates}개")
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
       l.average_view_duration, l.average_view_percentage,
       l.creator_content_type, l.shorts_feed_views, l.shorts_feed_engaged_views,
       l.performance_score,
       l.likes AS analytics_likes, l.comments AS analytics_comments,
       l.shares, l.subscribers_gained, l.subscribers_lost
FROM videos v
LEFT JOIN latest l ON l.video_id=v.video_id AND l.row_number=1
"""


def _row_sample(row: sqlite3.Row | dict[str, Any]) -> int:
    return max(row["engaged_views"] or 0, row["views"] or 0)


def _cohort_confidence(scored_count: int) -> str:
    if scored_count < MATURITY_LEARNING_VIDEOS:
        return "초기"
    if scored_count < MATURITY_GROWING_VIDEOS:
        return "학습 중"
    if scored_count < MATURITY_STABLE_VIDEOS:
        return "성장 중"
    return "안정화"


def _recalculate_latest_scores(conn: sqlite3.Connection) -> None:
    latest = conn.execute(
        "SELECT period_start, period_end FROM analytics "
        "ORDER BY period_end DESC, fetched_at DESC LIMIT 1"
    ).fetchone()
    if latest:
        calculate_performance_scores(conn, latest["period_start"], latest["period_end"])


def build_report_data(conn: sqlite3.Connection, strictness: str = "balanced") -> dict[str, Any]:
    strictness = normalize_strictness(strictness)
    profile = STRICTNESS_PROFILES[strictness]
    # Recompute cached periods so databases created by older score formulas are
    # safe to use even when the network refresh is temporarily unavailable.
    _recalculate_latest_scores(conn)
    rows = conn.execute(LATEST_ANALYTICS_SQL).fetchall()
    scored = sorted(
        (row for row in rows if row["performance_score"] is not None),
        key=lambda row: row["performance_score"],
        reverse=True,
    )
    samples = [_row_sample(row) for row in scored]
    sample_reliability = cohort_reliability(len(scored))
    relative_sample_floor = quantile(samples, 0.25) or 0.0
    top_count = min(10, max(1, math.ceil(len(scored) * profile["top_fraction"]))) if scored else 0
    strategy_thresholds = adaptive_strategy_thresholds(scored, strictness)
    analyzed_videos = []
    quadrant_summary = {
        key: {**value, "video_count": 0, "examples": []}
        for key, value in QUADRANT_STRATEGIES.items()
    }
    for row in scored:
        derived = derive_video_metrics(row)
        strategy = classify_video_strategy(derived, strategy_thresholds)
        entry = {
            "video_id": row["video_id"],
            "title": row["title"],
            "performance_score": round(row["performance_score"], 4),
            "sample": _row_sample(row),
            "low_confidence": _row_sample(row) < relative_sample_floor,
            "creator_content_type": row["creator_content_type"],
            "initial_engagement_rate": nullable_number(derived["initial_engagement_rate"], float),
            "initial_engagement_source": derived["initial_engagement_source"],
            "shorts_feed_view_share": nullable_number(derived["shorts_feed_view_share"], float),
            "average_view_duration": derived["average_view_duration"],
            "average_view_percentage": derived["average_view_percentage"],
            "subscriber_conversion_rate": nullable_number(derived["subscriber_conversion_rate"], float),
            "net_subscriber_conversion_rate": nullable_number(
                derived["net_subscriber_conversion_rate"], float
            ),
            "replay_lift": derived["replay_lift"],
            "topic_strategy": strategy,
        }
        analyzed_videos.append(entry)
        summary = quadrant_summary[strategy["quadrant"]]
        summary["video_count"] += 1
        if len(summary["examples"]) < 3:
            summary["examples"].append({
                "video_id": row["video_id"],
                "title": row["title"],
                "performance_score": round(row["performance_score"], 4),
            })
    top_videos = analyzed_videos[:top_count]

    raw_keyword_rows = conn.execute("""
        WITH latest AS (
            SELECT a.*, ROW_NUMBER() OVER (
                PARTITION BY video_id ORDER BY period_end DESC, fetched_at DESC
            ) AS row_number
            FROM analytics a
        )
        SELECT k.keyword, AVG(l.performance_score) AS avg_score, COUNT(DISTINCT k.video_id) AS video_count
        FROM keywords k JOIN latest l ON l.video_id=k.video_id AND l.row_number=1
        WHERE l.performance_score IS NOT NULL AND k.source='title'
          AND (l.creator_content_type='SHORTS' OR l.creator_content_type IS NULL)
        GROUP BY k.keyword
    """).fetchall()
    channel_center = statistics.median(
        float(row["performance_score"]) for row in scored
    ) if scored else 0.5
    prior_strength = float(profile["keyword_prior_strength"]) + (1.0 - sample_reliability) * 6.0
    keyword_min_count = max(2 if 0 < len(scored) < MATURITY_GROWING_VIDEOS else 1, math.ceil(len(scored) * float(profile["keyword_min_fraction"])))
    keyword_min_count = min(keyword_min_count, max(1, len(scored)))
    keyword_rows = []
    for row in raw_keyword_rows:
        count = int(row["video_count"])
        if count < keyword_min_count:
            continue
        average = float(row["avg_score"])
        adjusted = (average * count + channel_center * prior_strength) / (count + prior_strength)
        keyword_rows.append({
            "keyword": row["keyword"],
            "average_score": round(average, 4),
            "adjusted_score": round(adjusted, 4),
            "video_count": count,
            "support": round(count / (count + prior_strength), 4),
        })
    keyword_rows.sort(
        key=lambda row: (row["adjusted_score"], row["video_count"], row["keyword"]),
        reverse=True,
    )
    keyword_rows = keyword_rows[:15]
    recent = sorted(rows, key=lambda row: row["published_at"] or "", reverse=True)[:10]
    last_sync = conn.execute(
        "SELECT finished_at, video_count FROM sync_runs WHERE status='success' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    return {
        "generated_at": iso_now(),
        "analysis_period_days": ANALYSIS_PERIOD_DAYS,
        "video_count": len(rows),
        "scored_video_count": len(scored),
        "cohort_confidence": _cohort_confidence(len(scored)),
        "cohort_reliability": round(sample_reliability, 4),
        "strictness": strictness,
        "strictness_label": profile["label"],
        "relative_sample_floor": round(relative_sample_floor, 2),
        "keyword_min_count": keyword_min_count,
        "score_distribution": {
            "q25": round(quantile([row["performance_score"] for row in scored], 0.25) or 0.5, 4),
            "median": round(quantile([row["performance_score"] for row in scored], 0.50) or 0.5, 4),
            "q75": round(quantile([row["performance_score"] for row in scored], 0.75) or 0.5, 4),
        },
        "performance_weights": PERFORMANCE_WEIGHTS,
        "strategy_thresholds": strategy_thresholds,
        "quadrant_summary": quadrant_summary,
        "last_successful_sync": last_sync["finished_at"] if last_sync else None,
        "top_videos": top_videos,
        "low_sample_videos": [{
            "video_id": row["video_id"],
            "title": row["title"],
            "sample": _row_sample(row),
        } for row in scored if _row_sample(row) < relative_sample_floor],
        "preferred_keywords": [row["keyword"] for row in keyword_rows],
        "keyword_performance": keyword_rows,
        "recent_topics": [row["title"] for row in recent],
        "notes": [
            "\uc870\ud68c\uc218 \ub2e8\ub3c5 \uae30\uc900\uc73c\ub85c \ubc29\ud5a5\uc744 \ubcc0\uacbd\ud558\uc9c0 \uc54a\uc74c",
            "\ucc44\ub110 \ub0b4\ubd80 \ubd84\uc704\uc218\ub85c \uc815\uaddc\ud654\ud558\uba70 \ud45c\ubcf8\uc774 \uc313\uc77c\uc218\ub85d \uae30\uc900\uc774 \uc790\ub3d9 \uac31\uc2e0\ub428",
            "\uc791\uc740 \ud45c\ubcf8\uc740 \ucc44\ub110 \uc911\uc559\uac12 \ucabd\uc73c\ub85c \ucd95\uc18c\ud574 \ud2b9\uc774\uac12 \uacfc\ub300 \ud574\uc11d\uc744 \uc904\uc784",
            "YouTube Analytics API\ub294 \uc1fc\uce20 \ud53c\ub4dc \ub178\ucd9c\uc218\ub97c \uc81c\uacf5\ud558\uc9c0 \uc54a\uc73c\uba70, \ud53c\ub4dc \uc720\uc785 views \ub300\ube44 engagedViews\ub97c \ucd08\ubc18 \ubab0\uc785 \ub300\ub9ac\uc9c0\ud45c\ub85c \uc0ac\uc6a9\ud568",
        ],
    }


def render_markdown(data: dict[str, Any]) -> str:
    last_sync = data["last_successful_sync"] or "\uc5c6\uc74c"
    lines = [
        "# YouTube \ucc44\ub110 \ud53c\ub4dc\ubc31 \ubcf4\uace0\uc11c", "",
        f"- \uc0dd\uc131 \uc2dc\uac01: {data['generated_at']}",
        f"- \ub9c8\uc9c0\ub9c9 \uc815\uc0c1 \ub3d9\uae30\ud654: {last_sync}",
        f"- \ubd84\uc11d \uc601\uc0c1 \uc218: {data['video_count']}\uac1c",
        f"- \uc131\uacfc \ube44\uad50 \uac00\ub2a5 \uc601\uc0c1: {data['scored_video_count']}\uac1c ({data['cohort_confidence']})",
        f"- \ubd84\uc11d \uae30\uac04: \ucd5c\uadfc {data['analysis_period_days']}\uc77c",
        f"- \ud310\ub2e8 \uac15\ub3c4: {data['strictness_label']}",
        f"- \ucc44\ub110 \uc0c1\ub300 \ud45c\ubcf8 \ud558\uc704 25% \uae30\uc900: {data['relative_sample_floor']:.0f}\ud68c",
        f"- 4\ubd84\uba74 \uc801\uc751 \uae30\uc900: \uc2dc\uccad \uc9c0\uc18d\ub960 {data['strategy_thresholds']['retention_percentage']:.1f}%, "
        f"\uad6c\ub3c5 \uc804\ud658\uc728 {data['strategy_thresholds']['subscriber_conversion_rate']:.2%}", "",
        "## \uc0c1\ub300 \uc131\uacfc \uc0c1\uc704 \uc601\uc0c1", "",
        "| \uc21c\uc704 | \uc81c\ubaa9 | \uc810\uc218 | \ucd08\ubc18 \ubab0\uc785 | \uc9c0\uc18d\ub960 | \uad6c\ub3c5 \uc804\ud658 | \uc804\ub7b5 |",
        "|---:|---|---:|---:|---:|---:|---|",
    ]
    if data["top_videos"]:
        for index, video in enumerate(data["top_videos"], 1):
            title = video["title"].replace("|", "\\|").replace("\n", " ")
            hook = f"{video['initial_engagement_rate']:.1%}" if video["initial_engagement_rate"] is not None else "-"
            retention = f"{video['average_view_percentage']:.1f}%" if video["average_view_percentage"] is not None else "-"
            conversion = f"{video['subscriber_conversion_rate']:.2%}" if video["subscriber_conversion_rate"] is not None else "-"
            strategy = video["topic_strategy"]["label"]
            lines.append(
                f"| {index} | {title} | {video['performance_score']:.3f} | "
                f"{hook} | {retention} | {conversion} | {strategy} |"
            )
    else:
        lines.append("| - | Analytics \uc131\uacfc \ub370\uc774\ud130 \uc5c6\uc74c | - | - | - | - | - |")

    lines.extend(["", "## 4\ubd84\uba74 \uc8fc\uc81c \uc804\ub7b5", ""])
    for quadrant in ("Q1", "Q2", "Q3", "Q4", "NA"):
        summary = data["quadrant_summary"][quadrant]
        examples = " / ".join(item["title"] for item in summary["examples"]) or "\uc5c6\uc74c"
        lines.append(
            f"- {quadrant} {summary['label']}: {summary['video_count']}\uac1c \u2014 "
            f"{summary['instruction']} (\uc608: {examples})"
        )

    lines.extend(["", "## \ucd5c\uc18c \ud45c\ubcf8 \ubbf8\ub2ec \uc601\uc0c1", ""])
    if data["low_sample_videos"]:
        for video in data["low_sample_videos"]:
            lines.append(f"- {video['title']} \u2014 \ud45c\ubcf8 {video['sample']:,}\ud68c")
    else:
        lines.append("- \uc5c6\uc74c")

    lines.extend([
        "", "## \uc131\uacfc\uac00 \ub192\uc740 \ud0a4\uc6cc\ub4dc", "",
        f"- \ucc44\ub110 \uc801\uc751 \ucd5c\uc18c \ubc18\ubcf5: {data['keyword_min_count']}\uac1c \uc601\uc0c1", "",
    ])
    if data["keyword_performance"]:
        for keyword in data["keyword_performance"]:
            lines.append(
                f"- {keyword['keyword']} \u2014 \ubcf4\uc815 \uc810\uc218 {keyword['adjusted_score']:.3f}, "
                f"\uc601\uc0c1 {keyword['video_count']}\uac1c, \uc9c0\uc9c0\ub3c4 {keyword['support']:.0%}"
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


def write_reports(
    conn: sqlite3.Connection,
    strictness: str = "balanced",
) -> tuple[Path, Path, dict[str, Any]]:
    data = build_report_data(conn, strictness)
    markdown_target = report_path()
    json_target = strategy_path()
    markdown_target.parent.mkdir(parents=True, exist_ok=True)
    json_target.parent.mkdir(parents=True, exist_ok=True)
    markdown_target.write_text(render_markdown(data), encoding="utf-8")
    json_target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return markdown_target, json_target, data


def cmd_report(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        markdown_target, json_target, data = write_reports(conn, getattr(args, "strictness", "balanced"))
    finally:
        conn.close()
    print(f"\ubcf4\uace0\uc11c \uc0dd\uc131 \uc644\ub8cc: \uc601\uc0c1 {data['video_count']}\uac1c")
    print(f"Markdown: {markdown_target}")
    print(f"JSON: {json_target}")
    return 0


def _video_keyword_sets(conn: sqlite3.Connection) -> dict[str, set[str]]:
    video_words: dict[str, set[str]] = defaultdict(set)
    for row in conn.execute("SELECT video_id, keyword FROM keywords WHERE source='title'"):
        video_words[row["video_id"]].add(row["keyword"])
    return video_words


def adaptive_topic_thresholds(
    conn: sqlite3.Connection,
    strictness: str = "balanced",
) -> dict[str, Any]:
    strictness = normalize_strictness(strictness)
    profile = STRICTNESS_PROFILES[strictness]
    word_sets = list(_video_keyword_sets(conn).values())
    positive_similarities = [
        similarity
        for index, left in enumerate(word_sets)
        for right in word_sets[index + 1:]
        if (similarity := jaccard(left, right)) > 0
    ]
    empirical_weight = cohort_reliability(len(word_sets))

    def blended(name: str) -> float:
        empirical = quantile(positive_similarities, float(profile[f"{name}_quantile"]))
        prior = float(profile[f"{name}_prior"])
        if empirical is None:
            return prior
        return prior * (1.0 - empirical_weight) + float(empirical) * empirical_weight

    review = blended("review")
    duplicate = max(blended("duplicate"), review + 0.05)
    return {
        "review": round(min(review, 0.95), 4),
        "duplicate": round(min(duplicate, 1.0), 4),
        "baseline_video_count": len(word_sets),
        "positive_pair_count": len(positive_similarities),
        "empirical_weight": round(empirical_weight, 4),
    }


def find_similar_topics(
    conn: sqlite3.Connection,
    topic: str,
    limit: int = 5,
    strictness: str = "balanced",
) -> list[dict[str, Any]]:
    topic_words = normalize_keywords(topic)
    video_words = _video_keyword_sets(conn)
    thresholds = adaptive_topic_thresholds(conn, strictness)
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
            "verdict": topic_verdict(similarity, thresholds),
            "common_keywords": sorted(topic_words & words),
        })
    return sorted(
        results,
        key=lambda item: (item["similarity"], item["published_at"] or ""),
        reverse=True,
    )[:limit]


def build_content_guidance(
    conn: sqlite3.Connection,
    topic: str,
    strictness: str = "balanced",
) -> dict[str, Any]:
    strictness = normalize_strictness(strictness)
    report = build_report_data(conn, strictness)
    thresholds = adaptive_topic_thresholds(conn, strictness)
    similar = find_similar_topics(conn, topic, strictness=strictness)
    max_similarity = float(similar[0]["similarity"]) if similar else 0.0
    verdict = topic_verdict(max_similarity, thresholds)
    if not normalize_keywords(topic):
        verdict = "검토"

    top_videos = report["top_videos"][:5]
    keywords = report["keyword_performance"][:8]
    strategy_thresholds = report["strategy_thresholds"]
    quadrant_summary = report["quadrant_summary"]
    lines = [
        "[YouTube 채널 실데이터 인사이트]",
        f"- 판단 강도: {report['strictness_label']} ({strictness})",
        f"- 비교 표본: 성과 영상 {report['scored_video_count']}개 / 전체 영상 {report['video_count']}개; "
        f"신뢰 단계={report['cohort_confidence']}, 반영도={report['cohort_reliability']:.0%}",
        f"- 새 주제 중복 판단: {verdict}; 최고 유사도={max_similarity:.1%}; "
        f"채널 적응 기준 검토={thresholds['review']:.1%}, 중복={thresholds['duplicate']:.1%}",
        f"- 쇼츠 전략 기준: 평균 시청 지속률 {strategy_thresholds['retention_percentage']:.1f}% / "
        f"조회수 대비 구독 전환율 {strategy_thresholds['subscriber_conversion_rate']:.2%}; "
        "채널 데이터와 85%·0.5% 초기 기준을 표본 수에 따라 혼합",
    ]
    if similar and max_similarity > 0:
        lines.append("- 가까운 기존 영상: " + " / ".join(
            f"{item['title']} ({item['similarity']:.0%})" for item in similar[:3]
        ))
    if top_videos:
        def metric_text(item: dict[str, Any]) -> str:
            hook = (
                f"{item['initial_engagement_rate']:.0%}"
                if item["initial_engagement_rate"] is not None else "-"
            )
            retention = (
                f"{item['average_view_percentage']:.1f}%"
                if item["average_view_percentage"] is not None else "-"
            )
            conversion = (
                f"{item['subscriber_conversion_rate']:.2%}"
                if item["subscriber_conversion_rate"] is not None else "-"
            )
            return (
                f"{item['title']} [점수={item['performance_score']:.3f}, "
                f"초반몰입={hook}, 지속률={retention}, 구독={conversion}, "
                f"{item['topic_strategy']['quadrant']}]"
            )

        lines.append("- 채널 내 상대 성과 상위 쇼츠: " + " / ".join(
            metric_text(item) for item in top_videos
        ))
    active_quadrants = []
    for quadrant in ("Q1", "Q2", "Q3", "Q4"):
        summary = quadrant_summary[quadrant]
        if not summary["video_count"]:
            continue
        examples = ", ".join(item["title"] for item in summary["examples"][:2])
        active_quadrants.append(
            f"{quadrant} {summary['label']} {summary['video_count']}개"
            f"({examples}): {summary['instruction']}"
        )
    if active_quadrants:
        lines.append("- 4분면 실전 지침: " + " / ".join(active_quadrants))
    if keywords:
        lines.append("- 성과 연관 키워드(작은 표본은 중앙값으로 보정): " + ", ".join(
            f"{item['keyword']}[{item['adjusted_score']:.3f}, n={item['video_count']}]"
            for item in keywords
        ))

    if verdict == "중복 가능성 높음":
        lines.append("- 전략 지침: 같은 결론·제목 구조를 반복하지 말고, 시청자 질문이나 해결 각도를 명확히 바꿀 것.")
    elif verdict == "검토":
        lines.append("- 전략 지침: 겹치는 핵심어는 활용할 수 있지만 기존 영상과 다른 질문·사례·실천 답을 제시할 것.")
    else:
        lines.append("- 전략 지침: 새 주제로 진행하되 상위 콘텐츠의 명료한 제목 약속과 핵심어 사용 방식을 참고할 것.")
    lines.extend([
        "- 성과 데이터는 방향 선택에만 사용하고 의학적 사실과 결론은 PubMed·검증 근거를 우선할 것.",
        "- 조회수 하나를 성공 공식으로 일반화하지 말고, 채널 상대 성과와 표본 신뢰도를 함께 고려할 것.",
    ])
    return {
        "available": bool(report["video_count"]),
        "generated_at": iso_now(),
        "topic": topic,
        "strictness": strictness,
        "strictness_label": report["strictness_label"],
        "cohort": {
            "video_count": report["video_count"],
            "scored_video_count": report["scored_video_count"],
            "confidence": report["cohort_confidence"],
            "reliability": report["cohort_reliability"],
            "score_distribution": report["score_distribution"],
        },
        "topic_assessment": {
            "verdict": verdict,
            "max_similarity": round(max_similarity, 4),
            "thresholds": thresholds,
            "similar_videos": similar,
        },
        "top_videos": top_videos,
        "preferred_keywords": keywords,
        "strategy_thresholds": strategy_thresholds,
        "quadrant_summary": quadrant_summary,
        "prompt_text": "\n".join(lines),
    }


def _env_enabled(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return value.strip().lower() not in {"0", "false", "off", "no", "n"}


def successful_sync_age_hours(conn: sqlite3.Connection, now: datetime | None = None) -> float | None:
    row = conn.execute(
        "SELECT finished_at FROM sync_runs WHERE status='success' ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    if not row or not row["finished_at"]:
        return None
    try:
        finished = datetime.fromisoformat(str(row["finished_at"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    current = now or datetime.now(timezone.utc)
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    return max(0.0, (current - finished).total_seconds() / 3600.0)


def feedback_cache_status(
    conn: sqlite3.Connection,
    *,
    ttl_hours: int = 6,
    stale_hours: int = 24 * 7,
) -> str:
    age = successful_sync_age_hours(conn)
    if age is None:
        return "missing"
    if age <= ttl_hours:
        return "fresh-cache"
    if age > stale_hours:
        return "stale-cache"
    return "refresh-needed"


def prepare_content_guidance(
    topic: str,
    strictness: str | None = None,
    auto_sync: bool | None = None,
) -> dict[str, Any]:
    strictness = normalize_strictness(strictness or os.environ.get("YOUTUBE_FEEDBACK_STRICTNESS"))
    if auto_sync is None:
        auto_sync = _env_enabled("YOUTUBE_FEEDBACK_AUTO_SYNC", True)
    token = _env_path("YOUTUBE_FEEDBACK_TOKEN")
    sync_status = "skipped"
    conn = connect()
    try:
        cache_status = feedback_cache_status(
            conn, ttl_hours=int(os.environ.get("YOUTUBE_FEEDBACK_SYNC_TTL_HOURS", "6"))
        )
    finally:
        conn.close()
    if cache_status == "fresh-cache":
        sync_status = cache_status
    elif auto_sync and token is not None and token.is_file():
        if cmd_sync(argparse.Namespace()) == 0:
            sync_status = "success"
        else:
            retry_conn = connect()
            try:
                after_failure = feedback_cache_status(
                    retry_conn,
                    ttl_hours=int(os.environ.get("YOUTUBE_FEEDBACK_SYNC_TTL_HOURS", "6")),
                )
            finally:
                retry_conn.close()
            sync_status = (
                "stale-cache" if after_failure in {"stale-cache", "missing"}
                else "refresh-failed-cache"
            )

    conn = connect()
    try:
        guidance = build_content_guidance(conn, topic, strictness)
        write_reports(conn, strictness)
        final_cache_status = feedback_cache_status(
            conn, ttl_hours=int(os.environ.get("YOUTUBE_FEEDBACK_SYNC_TTL_HOURS", "6"))
        )
    finally:
        conn.close()
    guidance["sync_status"] = sync_status
    guidance["cache_status"] = final_cache_status
    return guidance


def cmd_check_topic(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        strictness = normalize_strictness(getattr(args, "strictness", "balanced"))
        thresholds = adaptive_topic_thresholds(conn, strictness)
        results = find_similar_topics(conn, args.topic, strictness=strictness)
    finally:
        conn.close()
    print(f"\uc785\ub825 \uc8fc\uc81c: {args.topic}")
    if not normalize_keywords(args.topic):
        print("\ud310\uc815: \uac80\ud1a0 (\ube44\uad50\ud560 \uc720\ud6a8 \ud0a4\uc6cc\ub4dc\uac00 \uc5c6\uc2b5\ub2c8\ub2e4.)")
        return 0
    overall = topic_verdict(results[0]["similarity"] if results else 0.0, thresholds)
    print(f"\ud310\uc815: {overall}")
    print(
        f"\ucc44\ub110 \uc801\uc751 \uae30\uc900: \uac80\ud1a0 {thresholds['review']:.1%}, "
        f"\uc911\ubcf5 {thresholds['duplicate']:.1%} ({STRICTNESS_PROFILES[strictness]['label']})"
    )
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


def cmd_guide(args: argparse.Namespace) -> int:
    guidance = prepare_content_guidance(
        args.topic,
        strictness=args.strictness,
        auto_sync=not args.no_sync,
    )
    print(guidance["prompt_text"])
    return 0


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="YouTube API \ud53c\ub4dc\ubc31 MVP")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("sync", help="\ucc44\ub110 \uc601\uc0c1\uacfc \ucd5c\uadfc Analytics\ub97c \ub3d9\uae30\ud654")
    report_parser = subparsers.add_parser("report", help="Markdown/JSON \ubcf4\uace0\uc11c \uc0dd\uc131")
    report_parser.add_argument("--strictness", default="balanced", choices=tuple(STRICTNESS_PROFILES))
    topic_parser = subparsers.add_parser("check-topic", help="\uae30\uc874 \uc601\uc0c1\uacfc \uc0c8 \uc8fc\uc81c\uc758 \uc911\ubcf5 \uac00\ub2a5\uc131 \ud655\uc778")
    topic_parser.add_argument("topic", help="\uac80\uc0ac\ud560 \uc0c8 \uc8fc\uc81c")
    topic_parser.add_argument("--strictness", default="balanced", choices=tuple(STRICTNESS_PROFILES))
    guide_parser = subparsers.add_parser("guide", help="\uc0c8 \uc8fc\uc81c\uc6a9 \uc2e4\ub370\uc774\ud130 \uc804\ub7b5 \uc0dd\uc131")
    guide_parser.add_argument("topic", help="\ubd84\uc11d\ud560 \uc0c8 \uc8fc\uc81c")
    guide_parser.add_argument("--strictness", default="balanced", choices=tuple(STRICTNESS_PROFILES))
    guide_parser.add_argument("--no-sync", action="store_true", help="API \ub3d9\uae30\ud654 \uc5c6\uc774 \uae30\uc874 DB \uc0ac\uc6a9")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            reconfigure(encoding="utf-8", errors="replace")
    args = parse_args(argv)
    commands = {
        "sync": cmd_sync,
        "report": cmd_report,
        "check-topic": cmd_check_topic,
        "guide": cmd_guide,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
