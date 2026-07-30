import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dev" / "src" / "common"
sys.path.insert(0, str(SRC))

import job_state


class JobStateTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_load_returns_defaults_when_file_absent(self):
        state = job_state.load(self.work_dir)
        self.assertIsNone(state["stage"])
        self.assertIsNone(state["awaiting"])
        self.assertEqual(state["attempts"], {})
        self.assertEqual(state["mode"], job_state.MODE_REVIEW)

    def test_save_and_load_roundtrip(self):
        job_state.update(self.work_dir, stage="tts", mode=job_state.MODE_AUTO)
        state = job_state.load(self.work_dir)
        self.assertEqual(state["stage"], "tts")
        self.assertEqual(state["mode"], job_state.MODE_AUTO)

    def test_corrupt_state_falls_back_to_defaults(self):
        # An unattended job must not be stranded by a half-written file.
        job_state.state_path(self.work_dir).write_text("{ not json", encoding="utf-8")
        state = job_state.load(self.work_dir)
        self.assertIsNone(state["stage"])
        self.assertEqual(state["attempts"], {})

    def test_missing_keys_are_filled_in(self):
        job_state.state_path(self.work_dir).write_text(
            json.dumps({"stage": "broll"}), encoding="utf-8"
        )
        state = job_state.load(self.work_dir)
        self.assertEqual(state["stage"], "broll")
        self.assertEqual(state["attempts"], {})
        self.assertIsNone(state["awaiting"])

    def test_record_and_reset_attempts(self):
        self.assertEqual(job_state.record_attempt(self.work_dir, "broll"), 1)
        self.assertEqual(job_state.record_attempt(self.work_dir, "broll"), 2)
        self.assertEqual(job_state.attempts_for(self.work_dir, "broll"), 2)
        job_state.reset_attempts(self.work_dir, "broll")
        self.assertEqual(job_state.attempts_for(self.work_dir, "broll"), 0)

    def test_reset_all_attempts(self):
        job_state.record_attempt(self.work_dir, "tts")
        job_state.record_attempt(self.work_dir, "broll")
        job_state.reset_attempts(self.work_dir)
        self.assertEqual(job_state.load(self.work_dir)["attempts"], {})

    def test_park_and_resume(self):
        job_state.park(self.work_dir, "script_review", stage="script")
        self.assertTrue(job_state.is_awaiting(self.work_dir))
        self.assertEqual(job_state.load(self.work_dir)["stage"], "script")
        job_state.resume(self.work_dir)
        self.assertFalse(job_state.is_awaiting(self.work_dir))

    def test_set_mode_rejects_unknown(self):
        with self.assertRaises(ValueError):
            job_state.set_mode(self.work_dir, "nope")

    def test_fail_records_reason_without_losing_progress(self):
        job_state.update(self.work_dir, stage="caption")
        job_state.fail(self.work_dir, "자막 정렬 실패")
        state = job_state.load(self.work_dir)
        self.assertEqual(state["stage"], "caption")
        self.assertEqual(state["last_error"], "자막 정렬 실패")

    def test_write_is_atomic_leaving_no_temp_file(self):
        job_state.update(self.work_dir, stage="tts")
        leftovers = list(self.work_dir.glob("*.tmp"))
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
