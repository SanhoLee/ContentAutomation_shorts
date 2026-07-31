import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dev" / "src" / "common"
sys.path.insert(0, str(SRC))
# dev/config.sh puts common/, youtube/ and instagram/ all on PYTHONPATH so
# the pipeline modules can import each other; tests must do the same.
sys.path.insert(0, str(ROOT / "dev" / "src" / "youtube"))

import job_state
import pipeline_flow


BASE_DIR = Path("/base")


class FakeRunner:
    """Records what would have run, and can be told to fail a given stage."""

    def __init__(self, fail_stages=()):
        self.calls = []
        self.fail_stages = set(fail_stages)

    def __call__(self, stage, argv):
        self.calls.append((stage, argv))
        if stage in self.fail_stages:
            raise RuntimeError(f"{stage} 명령 실패")

    @property
    def stages(self):
        return [stage for stage, _ in self.calls]


def guard_always_ok(stage, work_dir, settings=None):
    return True, ""


def guard_failing(*failing):
    """A guard that reports failure for the named stages, success otherwise."""
    failing = set(failing)

    def check(stage, work_dir, settings=None):
        if stage in failing:
            return False, f"{stage} 점검 실패"
        return True, ""

    return check


class PipelineFlowTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.work_dir = Path(self._tmp.name)
        self._real_check = pipeline_flow.stage_guard.check
        pipeline_flow.stage_guard.check = guard_always_ok

    def tearDown(self):
        pipeline_flow.stage_guard.check = self._real_check
        self._tmp.cleanup()

    def advance(self, runner, mode):
        return pipeline_flow.advance(self.work_dir, BASE_DIR, runner, mode=mode)


class GraphTests(unittest.TestCase):
    def test_stage_order_is_the_pipeline_order(self):
        self.assertEqual(
            pipeline_flow.STAGE_NAMES,
            ("script", "tts", "caption", "broll", "render", "upload"),
        )

    def test_review_mode_honours_exactly_two_gates(self):
        gates = pipeline_flow.gates_for_mode(job_state.MODE_REVIEW)
        self.assertEqual(gates, {"script_review", "final_confirm"})

    def test_auto_mode_honours_no_gates(self):
        self.assertEqual(pipeline_flow.gates_for_mode(job_state.MODE_AUTO), frozenset())

    def test_full_gate_mode_honours_every_gate(self):
        self.assertEqual(
            pipeline_flow.gates_for_mode(job_state.MODE_FULL_GATE),
            frozenset(pipeline_flow.ALL_GATES),
        )

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            pipeline_flow.gates_for_mode("sideways")

    def test_commands_resolve_against_base_dir(self):
        argv = pipeline_flow.resolve_command(BASE_DIR, ("sh/youtube/1_tts.sh",))
        self.assertEqual(argv, [str(BASE_DIR / "sh/youtube/1_tts.sh")])

    def test_bare_arguments_are_left_alone(self):
        argv = pipeline_flow.resolve_command(BASE_DIR, ("python3", "src/x.py"))
        self.assertEqual(argv, ["python3", str(BASE_DIR / "src/x.py")])

    def test_broll_retries_with_the_targeted_script(self):
        # A full re-run would re-query Pexels for every scene.
        stage = pipeline_flow.STAGES_BY_NAME["broll"]
        self.assertEqual(stage.retry_command, ("sh/youtube/1b_retry_broll.sh",))


class AutoModeTests(PipelineFlowTestCase):
    def test_runs_every_stage_without_stopping(self):
        runner = FakeRunner()
        result = self.advance(runner, job_state.MODE_AUTO)
        self.assertEqual(result.status, pipeline_flow.STATUS_DONE)
        self.assertEqual(runner.stages, list(pipeline_flow.STAGE_NAMES))


class ReviewModeTests(PipelineFlowTestCase):
    def test_stops_at_script_review_before_touching_tts(self):
        runner = FakeRunner()
        result = self.advance(runner, job_state.MODE_REVIEW)
        self.assertEqual(result.status, pipeline_flow.STATUS_GATE)
        self.assertEqual(result.gate, "script_review")
        self.assertEqual(runner.stages, ["script"])

    def test_second_call_without_approval_stays_parked(self):
        runner = FakeRunner()
        self.advance(runner, job_state.MODE_REVIEW)
        result = self.advance(runner, job_state.MODE_REVIEW)
        self.assertEqual(result.status, pipeline_flow.STATUS_GATE)
        self.assertEqual(runner.stages, ["script"])  # nothing new ran

    def test_after_approval_runs_through_to_the_final_gate(self):
        runner = FakeRunner()
        self.advance(runner, job_state.MODE_REVIEW)
        pipeline_flow.approve(self.work_dir)
        result = self.advance(runner, job_state.MODE_REVIEW)
        self.assertEqual(result.status, pipeline_flow.STATUS_GATE)
        self.assertEqual(result.gate, "final_confirm")
        self.assertEqual(runner.stages, ["script", "tts", "caption", "broll", "render"])

    def test_upload_only_happens_after_the_final_confirmation(self):
        runner = FakeRunner()
        self.advance(runner, job_state.MODE_REVIEW)
        pipeline_flow.approve(self.work_dir)
        self.advance(runner, job_state.MODE_REVIEW)
        self.assertNotIn("upload", runner.stages)

        pipeline_flow.approve(self.work_dir)
        result = self.advance(runner, job_state.MODE_REVIEW)
        self.assertEqual(result.status, pipeline_flow.STATUS_DONE)
        self.assertEqual(runner.stages[-1], "upload")

    def test_a_human_intervenes_exactly_twice(self):
        runner = FakeRunner()
        gates = []
        result = self.advance(runner, job_state.MODE_REVIEW)
        while result.status == pipeline_flow.STATUS_GATE:
            gates.append(result.gate)
            pipeline_flow.approve(self.work_dir)
            result = self.advance(runner, job_state.MODE_REVIEW)
        self.assertEqual(gates, ["script_review", "final_confirm"])
        self.assertEqual(result.status, pipeline_flow.STATUS_DONE)


class FullGateModeTests(PipelineFlowTestCase):
    def test_stops_after_every_stage(self):
        runner = FakeRunner()
        gates = []
        result = self.advance(runner, job_state.MODE_FULL_GATE)
        while result.status == pipeline_flow.STATUS_GATE:
            gates.append(result.gate)
            pipeline_flow.approve(self.work_dir)
            result = self.advance(runner, job_state.MODE_FULL_GATE)
        self.assertEqual(
            gates,
            ["script_review", "tts_review", "caption_review",
             "broll_review", "render_config", "final_confirm"],
        )
        self.assertEqual(runner.stages, list(pipeline_flow.STAGE_NAMES))

    def test_a_stage_with_two_gates_does_not_repeat_the_first(self):
        runner = FakeRunner()
        result = self.advance(runner, job_state.MODE_FULL_GATE)
        while result.gate != "broll_review":
            pipeline_flow.approve(self.work_dir)
            result = self.advance(runner, job_state.MODE_FULL_GATE)
        pipeline_flow.approve(self.work_dir)
        result = self.advance(runner, job_state.MODE_FULL_GATE)
        self.assertEqual(result.gate, "render_config")


class GuardRetryTests(PipelineFlowTestCase):
    def test_guard_failure_retries_the_stage_once(self):
        pipeline_flow.stage_guard.check = guard_failing("tts")
        runner = FakeRunner()
        result = self.advance(runner, job_state.MODE_AUTO)
        self.assertEqual(result.status, pipeline_flow.STATUS_FAILED)
        self.assertEqual(result.stage, "tts")
        self.assertEqual(runner.stages.count("tts"), pipeline_flow.MAX_ATTEMPTS_PER_STAGE)

    def test_failure_stops_the_pipeline_rather_than_continuing(self):
        pipeline_flow.stage_guard.check = guard_failing("caption")
        runner = FakeRunner()
        self.advance(runner, job_state.MODE_AUTO)
        self.assertNotIn("broll", runner.stages)
        self.assertNotIn("upload", runner.stages)

    def test_failure_reason_is_persisted_for_the_operator(self):
        pipeline_flow.stage_guard.check = guard_failing("broll")
        self.advance(FakeRunner(), job_state.MODE_AUTO)
        self.assertIn("broll 점검 실패", job_state.load(self.work_dir)["last_error"])

    def test_broll_retry_uses_the_targeted_command(self):
        pipeline_flow.stage_guard.check = guard_failing("broll")
        runner = FakeRunner()
        self.advance(runner, job_state.MODE_AUTO)
        broll_calls = [argv for stage, argv in runner.calls if stage == "broll"]
        self.assertEqual(len(broll_calls), 2)
        self.assertIn("1_broll.sh", broll_calls[0][0])
        self.assertIn("1b_retry_broll.sh", broll_calls[1][0])

    def test_a_stage_that_recovers_on_retry_continues(self):
        attempts = {"tts": 0}

        def flaky(stage, work_dir, settings=None):
            if stage != "tts":
                return True, ""
            attempts["tts"] += 1
            return attempts["tts"] > 1, "첫 시도 실패"

        pipeline_flow.stage_guard.check = flaky
        runner = FakeRunner()
        result = self.advance(runner, job_state.MODE_AUTO)
        self.assertEqual(result.status, pipeline_flow.STATUS_DONE)
        self.assertEqual(runner.stages.count("tts"), 2)

    def test_a_crashing_command_is_not_retried(self):
        # Re-running an identical command that errored just burns time and API
        # budget; stop and let a human look.
        runner = FakeRunner(fail_stages=["caption"])
        result = self.advance(runner, job_state.MODE_AUTO)
        self.assertEqual(result.status, pipeline_flow.STATUS_FAILED)
        self.assertEqual(runner.stages.count("caption"), 1)


class ResumeTests(PipelineFlowTestCase):
    def test_state_survives_a_fresh_process(self):
        # The loop-engineering case: one process parks the job, another picks
        # it up with nothing shared but the work directory.
        runner = FakeRunner()
        self.advance(runner, job_state.MODE_REVIEW)

        reloaded = job_state.load(self.work_dir)
        self.assertEqual(reloaded["stage"], "script")
        self.assertEqual(reloaded["awaiting"], "script_review")
        self.assertEqual(reloaded["mode"], job_state.MODE_REVIEW)

        pipeline_flow.approve(self.work_dir)
        later_runner = FakeRunner()
        result = pipeline_flow.advance(self.work_dir, BASE_DIR, later_runner)
        self.assertEqual(result.gate, "final_confirm")
        self.assertNotIn("script", later_runner.stages)

    def test_mode_defaults_to_the_stored_one(self):
        job_state.set_mode(self.work_dir, job_state.MODE_AUTO)
        result = pipeline_flow.advance(self.work_dir, BASE_DIR, FakeRunner())
        self.assertEqual(result.status, pipeline_flow.STATUS_DONE)

    def test_rewind_reruns_the_named_stage(self):
        runner = FakeRunner()
        self.advance(runner, job_state.MODE_REVIEW)
        pipeline_flow.approve(self.work_dir)
        self.advance(runner, job_state.MODE_REVIEW)

        pipeline_flow.rewind_to(self.work_dir, "script")
        again = FakeRunner()
        result = pipeline_flow.advance(self.work_dir, BASE_DIR, again)
        self.assertEqual(result.gate, "script_review")
        self.assertEqual(again.stages, ["script"])

    def test_rewind_clears_the_retry_budget(self):
        job_state.record_attempt(self.work_dir, "broll")
        job_state.record_attempt(self.work_dir, "broll")
        pipeline_flow.rewind_to(self.work_dir, "broll")
        self.assertEqual(job_state.attempts_for(self.work_dir, "broll"), 0)

    def test_rewind_rejects_unknown_stage(self):
        with self.assertRaises(ValueError):
            pipeline_flow.rewind_to(self.work_dir, "nope")


if __name__ == "__main__":
    unittest.main()
