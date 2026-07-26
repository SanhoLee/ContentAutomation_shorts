import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "dev" / "src" / "youtube" / "6_youtube_feedback.py"
SPEC = importlib.util.spec_from_file_location("youtube_feedback", MODULE_PATH)
youtube_feedback = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(youtube_feedback)


class YouTubeFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.db = self.root / "feedback.db"
        self.old_env = {
            key: os.environ.get(key)
            for key in (
                "YOUTUBE_FEEDBACK_DB",
                "YOUTUBE_FEEDBACK_REPORT",
                "YOUTUBE_FEEDBACK_STRATEGY",
            )
        }
        os.environ["YOUTUBE_FEEDBACK_DB"] = str(self.db)
        os.environ["YOUTUBE_FEEDBACK_REPORT"] = str(self.root / "report.md")
        os.environ["YOUTUBE_FEEDBACK_STRATEGY"] = str(self.root / "strategy.json")

    def tearDown(self):
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.temp_dir.cleanup()

    def sample_video(self, video_id="v1", title="\uc218\uba74 \ubd80\uc871\uacfc \uae30\uc5b5\ub825 \uc800\ud558"):
        return {
            "video_id": video_id,
            "title": title,
            "description": "\ubc24\uc5d0 \uc790\uc8fc \uae68\ub294 \uc2b5\uad00\uacfc \ub1cc \uac74\uac15\uc744 \uc124\uba85\ud569\ub2c8\ub2e4.",
            "published_at": "2026-07-01T00:00:00Z",
            "duration_seconds": 60,
            "view_count": 1000,
            "like_count": 100,
            "comment_count": 10,
            "fetched_at": "2026-07-18T00:00:00+00:00",
        }

    def test_duration_and_keyword_normalization(self):
        self.assertEqual(youtube_feedback.parse_duration_seconds("PT1M2S"), 62)
        words = youtube_feedback.normalize_keywords("50\ub300\uc758 \uc218\uba74 \ubd80\uc871\uacfc \uae30\uc5b5\ub825 \uc800\ud558 \ubc29\ubc95")
        self.assertIn("\uc218\uba74", words)
        self.assertIn("\uae30\uc5b5\ub825", words)
        self.assertNotIn("\ubc29\ubc95", words)

    def test_upsert_is_idempotent_and_preserves_missing_stats(self):
        conn = youtube_feedback.connect(self.db)
        video = self.sample_video()
        with conn:
            youtube_feedback.store_videos(conn, [video])
            changed = dict(video, title="\uc218\uba74\uacfc \uae30\uc5b5\ub825", view_count=None)
            youtube_feedback.store_videos(conn, [changed])
        row = conn.execute("SELECT title, view_count FROM videos").fetchone()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 1)
        self.assertEqual(row["title"], "\uc218\uba74\uacfc \uae30\uc5b5\ub825")
        self.assertEqual(row["view_count"], 1000)
        conn.close()

    def test_performance_score_renormalizes_zero_median_metrics(self):
        conn = youtube_feedback.connect(self.db)
        with conn:
            youtube_feedback.store_videos(
                conn,
                [self.sample_video("v1"), self.sample_video("v2", "\ud608\uc555\uacfc \uc218\uba74")],
            )
            youtube_feedback.store_analytics(
                conn,
                {
                    "v1": {"views": 1000, "averageViewPercentage": 80, "likes": 100, "shares": 0},
                    "v2": {"views": 1000, "averageViewPercentage": 40, "likes": 50, "shares": 0},
                },
                "2026-04-01", "2026-06-29", "2026-07-01T00:00:00+00:00",
            )
            youtube_feedback.calculate_performance_scores(conn, "2026-04-01", "2026-06-29")
        rows = conn.execute(
            "SELECT video_id, performance_score FROM analytics ORDER BY video_id"
        ).fetchall()
        self.assertGreater(rows[0]["performance_score"], rows[1]["performance_score"])
        self.assertIsNotNone(rows[0]["performance_score"])
        self.assertLess(rows[0]["performance_score"], 0.70)
        self.assertGreater(rows[1]["performance_score"], 0.30)
        conn.close()

    def test_shorts_feed_metrics_and_quadrant_are_derived_safely(self):
        conn = youtube_feedback.connect(self.db)
        with conn:
            youtube_feedback.store_videos(conn, [self.sample_video()])
            youtube_feedback.store_analytics(
                conn,
                {
                    "v1": {
                        "views": 1000,
                        "engagedViews": 700,
                        "averageViewDuration": 52,
                        "averageViewPercentage": 104,
                        "subscribersGained": 8,
                        "subscribersLost": 1,
                        "likes": 80,
                        "comments": 10,
                        "shares": 20,
                        "creatorContentType": "SHORTS",
                        "shortsFeedViews": 800,
                        "shortsFeedEngagedViews": 600,
                    },
                },
                "2026-04-01", "2026-06-29", "2026-07-01T00:00:00+00:00",
            )
            youtube_feedback.calculate_performance_scores(conn, "2026-04-01", "2026-06-29")
        row = conn.execute(youtube_feedback.LATEST_ANALYTICS_SQL).fetchone()
        derived = youtube_feedback.derive_video_metrics(row)
        self.assertAlmostEqual(derived["initial_engagement_rate"], 0.75)
        self.assertEqual(derived["initial_engagement_source"], "shorts_feed")
        self.assertAlmostEqual(derived["subscriber_conversion_rate"], 0.008)
        self.assertEqual(derived["replay_lift"], 4.0)

        thresholds = youtube_feedback.adaptive_strategy_thresholds([row])
        strategy = youtube_feedback.classify_video_strategy(derived, thresholds)
        self.assertEqual(strategy["quadrant"], "Q1")
        conn.close()

    def test_shorts_feed_query_keeps_only_official_shorts_traffic_rows(self):
        class Request:
            def execute(self):
                return {
                    "columnHeaders": [
                        {"name": "video"},
                        {"name": "insightTrafficSourceType"},
                        {"name": "views"},
                        {"name": "engagedViews"},
                    ],
                    "rows": [
                        ["v1", "SHORTS", 900, 600],
                        ["v1", "YT_SEARCH", 100, 80],
                    ],
                }

        class Reports:
            def query(self, **parameters):
                self.parameters = parameters
                return Request()

        class Analytics:
            def __init__(self):
                self.report_api = Reports()

            def reports(self):
                return self.report_api

        analytics = Analytics()
        result = youtube_feedback.fetch_shorts_feed_analytics(
            analytics, "2026-04-01", "2026-06-29", ["v1"]
        )
        self.assertEqual(result["v1"]["shortsFeedViews"], 900)
        self.assertEqual(result["v1"]["shortsFeedEngagedViews"], 600)
        self.assertEqual(result["v1"]["creatorContentType"], "SHORTS")
        self.assertEqual(
            analytics.report_api.parameters["dimensions"],
            "video,insightTrafficSourceType",
        )

    def test_explicit_long_form_rows_are_not_scored_as_shorts(self):
        conn = youtube_feedback.connect(self.db)
        with conn:
            youtube_feedback.store_videos(conn, [self.sample_video()])
            youtube_feedback.store_analytics(
                conn,
                {
                    "v1": {
                        "views": 1000,
                        "averageViewPercentage": 90,
                        "creatorContentType": "VIDEO_ON_DEMAND",
                    },
                },
                "2026-04-01", "2026-06-29", "2026-07-01T00:00:00+00:00",
            )
            youtube_feedback.calculate_performance_scores(conn, "2026-04-01", "2026-06-29")
        score = conn.execute("SELECT performance_score FROM analytics").fetchone()[0]
        self.assertIsNone(score)
        conn.close()

    def test_adaptive_thresholds_follow_strictness_and_channel_data(self):
        conn = youtube_feedback.connect(self.db)
        videos = [
            self.sample_video("v1", "수면 부족과 기억력 저하"),
            self.sample_video("v2", "수면 습관과 뇌 건강"),
            self.sample_video("v3", "혈압 관리와 걷기 운동"),
            self.sample_video("v4", "혈압 낮추는 저녁 습관"),
        ]
        with conn:
            youtube_feedback.store_videos(conn, videos)
        loose = youtube_feedback.adaptive_topic_thresholds(conn, "loose")
        strict = youtube_feedback.adaptive_topic_thresholds(conn, "strict")
        self.assertGreater(loose["review"], strict["review"])
        self.assertGreater(loose["duplicate"], strict["duplicate"])
        self.assertGreater(loose["empirical_weight"], 0)
        conn.close()

    def test_topic_check_and_reports(self):
        conn = youtube_feedback.connect(self.db)
        with conn:
            youtube_feedback.store_videos(conn, [self.sample_video()])
            youtube_feedback.store_analytics(
                conn,
                {
                    "v1": {
                        "views": 1000,
                        "engagedViews": 700,
                        "averageViewPercentage": 75,
                        "likes": 100,
                        "comments": 10,
                        "shares": 12,
                        "subscribersGained": 5,
                        "subscribersLost": 1,
                    },
                },
                "2026-04-01", "2026-06-29", "2026-07-01T00:00:00+00:00",
            )
            youtube_feedback.calculate_performance_scores(conn, "2026-04-01", "2026-06-29")
            conn.execute(
                "INSERT INTO sync_runs(started_at, finished_at, status, video_count) VALUES(?,?,?,?)",
                ("2026-07-01T00:00:00Z", "2026-07-01T00:01:00Z", "success", 1),
            )
        results = youtube_feedback.find_similar_topics(conn, "\uc218\uba74 \ubd80\uc871\uacfc \uae30\uc5b5\ub825 \uc800\ud558")
        self.assertEqual(results[0]["video_id"], "v1")
        self.assertGreater(results[0]["similarity"], 0)

        markdown, strategy, data = youtube_feedback.write_reports(conn)
        self.assertTrue(markdown.exists())
        self.assertTrue(strategy.exists())
        self.assertEqual(data["video_count"], 1)
        parsed = json.loads(strategy.read_text(encoding="utf-8"))
        self.assertEqual(parsed["top_videos"][0]["video_id"], "v1")
        self.assertEqual(parsed["strictness"], "balanced")
        self.assertIn("adjusted_score", parsed["keyword_performance"][0])
        self.assertIn("strategy_thresholds", parsed)
        self.assertIn("quadrant_summary", parsed)
        self.assertIn("\ub9c8\uc9c0\ub9c9 \uc815\uc0c1 \ub3d9\uae30\ud654", markdown.read_text(encoding="utf-8"))

        guidance = youtube_feedback.build_content_guidance(
            conn, "수면 부족과 기억력 저하", "balanced"
        )
        self.assertTrue(guidance["available"])
        self.assertIn("YouTube 채널 실데이터 인사이트", guidance["prompt_text"])
        self.assertIn("4분면", guidance["prompt_text"])
        self.assertEqual(guidance["strictness"], "balanced")
        conn.close()

    def test_migration_is_idempotent_enables_wal_and_preserves_data(self):
        conn = youtube_feedback.connect(self.db)
        with conn:
            youtube_feedback.store_videos(conn, [self.sample_video()])
        conn.close()
        conn = youtube_feedback.connect(self.db)
        self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
        self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0], 1)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("performance_snapshots", tables)
        self.assertIn("planning_runs", tables)
        self.assertIn("claude_usage", tables)
        conn.close()

    def test_snapshot_windows_and_video_linkage_remain_separate(self):
        conn = youtube_feedback.connect(self.db)
        with conn:
            youtube_feedback.store_videos(conn, [self.sample_video()])
            base = {
                "video_id": "v1", "period_start": "2026-07-01",
                "elapsed_days": 28, "views": 100, "engaged_views": 70,
                "fetched_at": "2026-07-20T00:00:00+00:00",
            }
            youtube_feedback.store_performance_snapshot(conn, {
                **base, "window_name": "D7", "period_end": "2026-07-07",
            })
            youtube_feedback.store_performance_snapshot(conn, {
                **base, "window_name": "D28", "period_end": "2026-07-28", "views": 250,
            })
            youtube_feedback.register_video_job(
                conn, job_id="job_1", topic="수면", plan_id=None, objective_id=None
            )
            youtube_feedback.link_uploaded_video(
                conn, job_id="job_1", video_id="uploaded_1",
                uploaded_at="2026-07-20T07:30:00+09:00", topic="수면",
                plan={"objective": {"type": "retention"}, "content_design": {"format_type": "자가진단형"}},
            )
        rows = conn.execute(
            "SELECT window_name, views FROM performance_snapshots WHERE video_id='v1' ORDER BY window_name"
        ).fetchall()
        self.assertEqual([(row["window_name"], row["views"]) for row in rows], [("D28", 250), ("D7", 100)])
        linked = conn.execute("SELECT video_id FROM video_jobs WHERE job_id='job_1'").fetchone()
        self.assertEqual(linked["video_id"], "uploaded_1")
        conn.close()


if __name__ == "__main__":
    unittest.main()
