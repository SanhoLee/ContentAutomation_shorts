import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

import schedule_policy as sp

SCHEDULE = {
    "timezone": "Asia/Seoul",
    "platforms": {
        "shorts": {"enabled": True, "times": ["10:35"], "daily_limit": 1},
        "x": {"enabled": True, "times": ["11:00"], "daily_limit": 2},
        "instagram": {"enabled": False, "times": ["10:30"], "daily_limit": 1},
    },
    "max_jobs_per_day": 3,
}

KST = ZoneInfo("Asia/Seoul")


class LoadScheduleTests(unittest.TestCase):
    def test_missing_file_degrades_to_all_disabled_default(self):
        schedule = sp.load_schedule("/no/such/schedules.yaml")
        self.assertEqual(schedule["platforms"], {})
        self.assertFalse(sp.can_run("shorts", schedule=schedule))

    def test_repo_config_file_loads(self):
        schedule = sp.load_schedule()
        self.assertIn("shorts", schedule["platforms"])
        self.assertEqual(schedule["timezone"], "Asia/Seoul")


class CanRunTests(unittest.TestCase):
    def test_disabled_platform_cannot_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(sp.can_run("instagram", schedule=SCHEDULE, log_path=Path(tmp) / "log.jsonl"))

    def test_unknown_platform_cannot_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(sp.can_run("wordpress", schedule=SCHEDULE, log_path=Path(tmp) / "log.jsonl"))

    def test_enabled_platform_with_quota_can_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(sp.can_run("shorts", schedule=SCHEDULE, log_path=Path(tmp) / "log.jsonl"))

    def test_daily_limit_blocks_after_quota_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            now = datetime(2026, 8, 2, 9, 0, tzinfo=KST)
            sp.record_publish("shorts", "J001", schedule=SCHEDULE, log_path=log_path, now=now)
            self.assertFalse(sp.can_run("shorts", now=now, schedule=SCHEDULE, log_path=log_path))

    def test_daily_limit_resets_on_a_new_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            day1 = datetime(2026, 8, 2, 9, 0, tzinfo=KST)
            day2 = datetime(2026, 8, 3, 9, 0, tzinfo=KST)
            sp.record_publish("shorts", "J001", schedule=SCHEDULE, log_path=log_path, now=day1)
            self.assertTrue(sp.can_run("shorts", now=day2, schedule=SCHEDULE, log_path=log_path))

    def test_global_max_jobs_per_day_blocks_even_under_platform_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            now = datetime(2026, 8, 2, 12, 0, tzinfo=KST)
            capped_schedule = {**SCHEDULE, "max_jobs_per_day": 1}
            sp.record_publish("x", "J001", schedule=capped_schedule, log_path=log_path, now=now)
            # x still has 1 of 2 daily_limit left, but the global cap of 1 job/day is spent.
            self.assertFalse(sp.can_run("x", now=now, schedule=capped_schedule, log_path=log_path))

    def test_zero_global_cap_means_uncapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            now = datetime(2026, 8, 2, 12, 0, tzinfo=KST)
            uncapped_schedule = {**SCHEDULE, "max_jobs_per_day": 0}
            sp.record_publish("shorts", "J001", schedule=uncapped_schedule, log_path=log_path, now=now)
            self.assertTrue(sp.can_run("x", now=now, schedule=uncapped_schedule, log_path=log_path))


class RemainingQuotaTests(unittest.TestCase):
    def test_full_quota_when_nothing_published(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(sp.remaining_quota("x", schedule=SCHEDULE, log_path=Path(tmp) / "log.jsonl"), 2)

    def test_quota_decreases_per_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            now = datetime(2026, 8, 2, 9, 0, tzinfo=KST)
            sp.record_publish("x", "J001", schedule=SCHEDULE, log_path=log_path, now=now)
            self.assertEqual(sp.remaining_quota("x", now.date(), schedule=SCHEDULE, log_path=log_path), 1)

    def test_never_goes_negative(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            now = datetime(2026, 8, 2, 9, 0, tzinfo=KST)
            sp.record_publish("shorts", "J001", schedule=SCHEDULE, log_path=log_path, now=now)
            sp.record_publish("shorts", "J002", schedule=SCHEDULE, log_path=log_path, now=now)
            self.assertEqual(sp.remaining_quota("shorts", now.date(), schedule=SCHEDULE, log_path=log_path), 0)


class NextSlotTests(unittest.TestCase):
    def test_returns_today_if_slot_still_ahead(self):
        now = datetime(2026, 8, 2, 9, 0, tzinfo=KST)
        slot = sp.next_slot("shorts", schedule=SCHEDULE, now=now)
        self.assertEqual(slot.date(), now.date())
        self.assertEqual((slot.hour, slot.minute), (10, 35))

    def test_rolls_to_tomorrow_once_todays_slot_passed(self):
        now = datetime(2026, 8, 2, 12, 0, tzinfo=KST)
        slot = sp.next_slot("shorts", schedule=SCHEDULE, now=now)
        self.assertEqual(slot.date().day, now.date().day + 1)

    def test_no_times_configured_returns_none(self):
        schedule = {**SCHEDULE, "platforms": {**SCHEDULE["platforms"], "shorts": {"enabled": True, "times": [], "daily_limit": 1}}}
        self.assertIsNone(sp.next_slot("shorts", schedule=schedule))


class RecordPublishTests(unittest.TestCase):
    def test_appends_jsonl_line_with_platform_and_job_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.jsonl"
            record = sp.record_publish("shorts", "J001", schedule=SCHEDULE, log_path=log_path)
            self.assertEqual(record["platform"], "shorts")
            self.assertEqual(record["job_id"], "J001")
            lines = log_path.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(len(lines), 1)

    def test_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "nested" / "ops" / "log.jsonl"
            sp.record_publish("shorts", "J001", schedule=SCHEDULE, log_path=log_path)
            self.assertTrue(log_path.exists())


if __name__ == "__main__":
    unittest.main()
