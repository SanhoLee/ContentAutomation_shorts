import json
import sys
import tempfile
import unittest
import wave
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dev" / "src" / "common"
sys.path.insert(0, str(SRC))
# dev/config.sh puts common/, youtube/ and instagram/ all on PYTHONPATH so
# the pipeline modules can import each other; tests must do the same.
sys.path.insert(0, str(ROOT / "dev" / "src" / "youtube"))

import stage_guard
from script_runtime import StageGuardSettings


SETTINGS = StageGuardSettings(
    target_duration_sec=60,
    tts_min_ratio=0.5,
    tts_max_ratio=1.8,
    caption_max_overrun_sec=3.0,
    broll_max_failed_ratio=0.3,
    render_duration_tolerance_sec=5.0,
)


def write_wav(path, seconds, rate=8000):
    """A silent PCM WAV of a known length, readable without ffprobe."""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


SRT = "1\n00:00:01,000 --> 00:00:03,000\n첫 자막\n"


class StageGuardTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class CheckTtsTests(StageGuardTestCase):
    def test_missing_voice_fails(self):
        ok, reason = stage_guard.check_tts(self.work_dir, SETTINGS)
        self.assertFalse(ok)
        self.assertIn("voice.wav", reason)

    def test_empty_voice_fails(self):
        (self.work_dir / "voice.wav").write_bytes(b"")
        ok, _ = stage_guard.check_tts(self.work_dir, SETTINGS)
        self.assertFalse(ok)

    def test_plausible_length_passes(self):
        write_wav(self.work_dir / "voice.wav", 58)
        ok, _ = stage_guard.check_tts(self.work_dir, SETTINGS)
        self.assertTrue(ok)

    def test_far_too_short_fails(self):
        write_wav(self.work_dir / "voice.wav", 5)
        ok, reason = stage_guard.check_tts(self.work_dir, SETTINGS)
        self.assertFalse(ok)
        self.assertIn("허용 범위", reason)

    def test_far_too_long_fails(self):
        write_wav(self.work_dir / "voice.wav", 200)
        ok, _ = stage_guard.check_tts(self.work_dir, SETTINGS)
        self.assertFalse(ok)

    def test_boundaries_are_inclusive(self):
        write_wav(self.work_dir / "voice.wav", 30)  # exactly 0.5x of 60
        self.assertTrue(stage_guard.check_tts(self.work_dir, SETTINGS)[0])
        write_wav(self.work_dir / "voice.wav", 108)  # exactly 1.8x of 60
        self.assertTrue(stage_guard.check_tts(self.work_dir, SETTINGS)[0])


class CheckCaptionTests(StageGuardTestCase):
    def prepare(self, scenes, srt=SRT, voice_seconds=60):
        write_wav(self.work_dir / "voice.wav", voice_seconds)
        (self.work_dir / "subs.srt").write_text(srt, encoding="utf-8")
        write_json(self.work_dir / "scenes_timed.json", scenes)

    def test_aligned_captions_pass(self):
        self.prepare([{"start": 0.0, "end": 30.0}, {"start": 30.0, "end": 59.0}])
        ok, _ = stage_guard.check_caption(self.work_dir, SETTINGS)
        self.assertTrue(ok)

    def test_missing_srt_fails(self):
        write_json(self.work_dir / "scenes_timed.json", [{"start": 0, "end": 1}])
        ok, reason = stage_guard.check_caption(self.work_dir, SETTINGS)
        self.assertFalse(ok)
        self.assertIn("subs.srt", reason)

    def test_srt_without_cues_fails(self):
        self.prepare([{"start": 0.0, "end": 10.0}], srt="자막 큐 없는 본문")
        ok, reason = stage_guard.check_caption(self.work_dir, SETTINGS)
        self.assertFalse(ok)
        self.assertIn("큐", reason)

    def test_empty_scenes_fails(self):
        self.prepare([])
        ok, reason = stage_guard.check_caption(self.work_dir, SETTINGS)
        self.assertFalse(ok)
        self.assertIn("scenes_timed.json", reason)

    def test_numbers_surviving_into_the_captions_pass(self):
        self.prepare(
            [{"start": 0.0, "end": 30.0}],
            srt="1\n00:00:01,000 --> 00:00:03,000\n고혈압 위험은\n1.32배였습니다\n",
        )
        (self.work_dir / "caption_script.txt").write_text(
            "고혈압 위험은 1.32배였습니다.", encoding="utf-8"
        )
        self.assertTrue(stage_guard.check_caption(self.work_dir, SETTINGS)[0])

    def test_mangled_decimal_fails(self):
        # A decimal turning into two numbers means the cited figure is now wrong.
        self.prepare(
            [{"start": 0.0, "end": 30.0}],
            srt="1\n00:00:01,000 --> 00:00:03,000\n고혈압 위험은\n1 32배였습니다\n",
        )
        (self.work_dir / "caption_script.txt").write_text(
            "고혈압 위험은 1.32배였습니다.", encoding="utf-8"
        )
        ok, reason = stage_guard.check_caption(self.work_dir, SETTINGS)
        self.assertFalse(ok)
        self.assertIn("숫자", reason)

    def test_number_check_is_skipped_without_the_source_script(self):
        self.prepare([{"start": 0.0, "end": 30.0}])
        self.assertTrue(stage_guard.check_caption(self.work_dir, SETTINGS)[0])

    def test_small_overrun_is_tolerated(self):
        self.prepare([{"start": 0.0, "end": 62.0}], voice_seconds=60)
        self.assertTrue(stage_guard.check_caption(self.work_dir, SETTINGS)[0])

    def test_large_overrun_fails(self):
        self.prepare([{"start": 0.0, "end": 90.0}], voice_seconds=60)
        ok, reason = stage_guard.check_caption(self.work_dir, SETTINGS)
        self.assertFalse(ok)
        self.assertIn("초과", reason)

    def test_passes_when_voice_length_unknown(self):
        # No voice.wav to compare against: report success rather than block.
        (self.work_dir / "subs.srt").write_text(SRT, encoding="utf-8")
        write_json(self.work_dir / "scenes_timed.json", [{"start": 0.0, "end": 999.0}])
        ok, reason = stage_guard.check_caption(self.work_dir, SETTINGS)
        self.assertTrue(ok)
        self.assertIn("생략", reason)


class CheckBrollTests(StageGuardTestCase):
    def prepare(self, statuses):
        write_json(
            self.work_dir / "broll_status.json",
            [{"index": i, "status": s} for i, s in enumerate(statuses)],
        )

    def test_all_ok_passes(self):
        self.prepare(["ok"] * 10)
        self.assertTrue(stage_guard.check_broll(self.work_dir, SETTINGS)[0])

    def test_missing_file_fails(self):
        ok, reason = stage_guard.check_broll(self.work_dir, SETTINGS)
        self.assertFalse(ok)
        self.assertIn("broll_status.json", reason)

    def test_a_few_failures_are_tolerated(self):
        self.prepare(["failed"] * 3 + ["ok"] * 7)  # 30%, at the limit
        self.assertTrue(stage_guard.check_broll(self.work_dir, SETTINGS)[0])

    def test_too_many_failures_fails(self):
        self.prepare(["failed"] * 4 + ["ok"] * 6)  # 40%, over the limit
        ok, reason = stage_guard.check_broll(self.work_dir, SETTINGS)
        self.assertFalse(ok)
        self.assertIn("허용치", reason)

    def test_retry_status_counts_as_success(self):
        # 3b_retry_broll.py marks recovered scenes "ok_retry", not "ok".
        self.prepare(["ok_retry"] * 5 + ["ok"] * 5)
        self.assertTrue(stage_guard.check_broll(self.work_dir, SETTINGS)[0])


class CheckRenderTests(StageGuardTestCase):
    def test_missing_marker_fails(self):
        ok, reason = stage_guard.check_render(self.work_dir, SETTINGS)
        self.assertFalse(ok)
        self.assertIn("output_path.txt", reason)

    def test_missing_output_fails(self):
        (self.work_dir / "output_path.txt").write_text(
            str(self.work_dir / "gone.mp4"), encoding="utf-8"
        )
        ok, reason = stage_guard.check_render(self.work_dir, SETTINGS)
        self.assertFalse(ok)
        self.assertIn("렌더 결과", reason)

    def test_matching_durations_pass(self):
        # A WAV stands in for the rendered file so the duration comparison can
        # be exercised without depending on ffprobe being installed.
        write_wav(self.work_dir / "voice.wav", 60)
        output = self.work_dir / "output.wav"
        write_wav(output, 61)
        (self.work_dir / "output_path.txt").write_text(str(output), encoding="utf-8")
        self.assertTrue(stage_guard.check_render(self.work_dir, SETTINGS)[0])

    def test_mismatched_durations_fail(self):
        write_wav(self.work_dir / "voice.wav", 60)
        output = self.work_dir / "output.wav"
        write_wav(output, 20)
        (self.work_dir / "output_path.txt").write_text(str(output), encoding="utf-8")
        ok, reason = stage_guard.check_render(self.work_dir, SETTINGS)
        self.assertFalse(ok)
        self.assertIn("허용치", reason)


class CheckDispatchTests(StageGuardTestCase):
    def test_unknown_stage_passes(self):
        ok, reason = stage_guard.check("upload", self.work_dir, SETTINGS)
        self.assertTrue(ok)
        self.assertEqual(reason, "")

    def test_dispatch_reaches_the_named_check(self):
        ok, reason = stage_guard.check("tts", self.work_dir, SETTINGS)
        self.assertFalse(ok)
        self.assertIn("voice.wav", reason)


if __name__ == "__main__":
    unittest.main()
