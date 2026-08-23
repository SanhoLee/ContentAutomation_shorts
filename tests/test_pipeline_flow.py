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

    def advance_past_x_photo_gate(self, runner, mode=job_state.MODE_AUTO):
        """Clears the x_photo_intake and thumbnail_intake gates that now sit
        back-to-back before tts/caption/broll, so guard-retry tests can reach
        the stage they're actually testing."""
        result = self.advance(runner, mode)
        self.assertEqual(result.gate, "x_photo_intake")
        pipeline_flow.approve(self.work_dir)
        result = self.advance(runner, mode)
        self.assertEqual(result.gate, "thumbnail_intake")
        pipeline_flow.approve(self.work_dir)


class GraphTests(unittest.TestCase):
    def test_stage_order_is_the_pipeline_order(self):
        self.assertEqual(
            pipeline_flow.STAGE_NAMES,
            ("script", "scene_visuals", "x_thread", "tts", "caption", "broll", "render",
             "upload", "x_post"),
        )

    def test_scene_visuals_sits_behind_the_script_gate_with_no_gate_of_its_own(self):
        # advance() parks *after* a stage, so placing it next to script is what
        # guarantees it reads the text the operator approved -- and it must not
        # add a stop of its own between that approval and the render.
        self.assertEqual(
            pipeline_flow.next_stage_after("script"),
            pipeline_flow.STAGES_BY_NAME["scene_visuals"],
        )
        self.assertEqual(pipeline_flow.STAGES_BY_NAME["scene_visuals"].gates, ())

    def test_x_thread_has_both_image_intake_gates_and_x_post_has_none(self):
        # x_thread's lead-photo intake and the video-thumbnail intake are both
        # deliberate gates the operator decides synchronously, back-to-back;
        # x_post remains best-effort and must never block on its own failure.
        self.assertEqual(
            pipeline_flow.STAGES_BY_NAME["x_thread"].gates,
            ("x_photo_intake", "thumbnail_intake"),
        )
        self.assertEqual(pipeline_flow.STAGES_BY_NAME["broll"].gates, ("broll_review", "render_config"))
        self.assertEqual(pipeline_flow.STAGES_BY_NAME["x_post"].gates, ())

    def test_review_mode_honours_exactly_five_gates(self):
        gates = pipeline_flow.gates_for_mode(job_state.MODE_REVIEW)
        self.assertEqual(
            gates,
            {"script_review", "x_thread_confirm", "x_photo_intake", "thumbnail_intake", "final_confirm"},
        )

    def test_auto_mode_honours_only_the_two_intake_gates(self):
        # Every other gate is skipped for unattended runs; the two intake
        # gates are the deliberate exceptions -- they still park for a human.
        self.assertEqual(
            pipeline_flow.gates_for_mode(job_state.MODE_AUTO),
            {"thumbnail_intake", "x_photo_intake"},
        )

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
    def test_stops_first_at_x_photo_intake(self):
        runner = FakeRunner()
        result = self.advance(runner, job_state.MODE_AUTO)
        self.assertEqual(result.status, pipeline_flow.STATUS_GATE)
        self.assertEqual(result.gate, "x_photo_intake")
        self.assertEqual(runner.stages, ["script", "scene_visuals", "x_thread"])

    def test_stops_second_at_thumbnail_intake(self):
        runner = FakeRunner()
        self.advance(runner, job_state.MODE_AUTO)
        pipeline_flow.approve(self.work_dir)
        result = self.advance(runner, job_state.MODE_AUTO)
        self.assertEqual(result.status, pipeline_flow.STATUS_GATE)
        self.assertEqual(result.gate, "thumbnail_intake")
        self.assertEqual(runner.stages, ["script", "scene_visuals", "x_thread"])

    def test_runs_every_stage_without_stopping_again_after_both_approvals(self):
        runner = FakeRunner()
        self.advance(runner, job_state.MODE_AUTO)
        pipeline_flow.approve(self.work_dir)
        self.advance(runner, job_state.MODE_AUTO)
        pipeline_flow.approve(self.work_dir)
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

    def test_after_script_approval_stops_at_x_thread_confirm(self):
        runner = FakeRunner()
        self.advance(runner, job_state.MODE_REVIEW)
        pipeline_flow.approve(self.work_dir)
        result = self.advance(runner, job_state.MODE_REVIEW)
        self.assertEqual(result.status, pipeline_flow.STATUS_GATE)
        self.assertEqual(result.gate, "x_thread_confirm")
        # x_thread_confirm rides the script gate's own list, so it parks
        # before scene_visuals/x_thread ever run -- the whole point is
        # deciding whether x_thread's API call happens at all.
        self.assertEqual(runner.stages, ["script"])

    def test_after_x_thread_confirm_stops_at_x_photo_intake(self):
        runner = FakeRunner()
        self.advance(runner, job_state.MODE_REVIEW)
        pipeline_flow.approve(self.work_dir)
        self.advance(runner, job_state.MODE_REVIEW)
        pipeline_flow.approve(self.work_dir)
        result = self.advance(runner, job_state.MODE_REVIEW)
        self.assertEqual(result.status, pipeline_flow.STATUS_GATE)
        self.assertEqual(result.gate, "x_photo_intake")
        # scene_visuals and x_thread only run once x_thread_confirm clears,
        # so both see the approved text by the time x_photo_intake asks
        # about the lead photo.
        self.assertEqual(runner.stages, ["script", "scene_visuals", "x_thread"])

    def test_after_x_photo_intake_stops_at_thumbnail_intake_before_render(self):
        runner = FakeRunner()
        self.advance(runner, job_state.MODE_REVIEW)
        pipeline_flow.approve(self.work_dir)
        self.advance(runner, job_state.MODE_REVIEW)
        pipeline_flow.approve(self.work_dir)
        self.advance(runner, job_state.MODE_REVIEW)
        pipeline_flow.approve(self.work_dir)
        result = self.advance(runner, job_state.MODE_REVIEW)
        self.assertEqual(result.status, pipeline_flow.STATUS_GATE)
        self.assertEqual(result.gate, "thumbnail_intake")
        # thumbnail_intake now rides x_thread's gate list right after
        # x_photo_intake, so tts/caption/broll haven't run yet.
        self.assertEqual(runner.stages, ["script", "scene_visuals", "x_thread"])

    def test_after_thumbnail_intake_runs_through_to_the_final_gate(self):
        runner = FakeRunner()
        self.advance(runner, job_state.MODE_REVIEW)
        pipeline_flow.approve(self.work_dir)
        self.advance(runner, job_state.MODE_REVIEW)
        pipeline_flow.approve(self.work_dir)
        self.advance(runner, job_state.MODE_REVIEW)
        pipeline_flow.approve(self.work_dir)
        self.advance(runner, job_state.MODE_REVIEW)
        pipeline_flow.approve(self.work_dir)
        result = self.advance(runner, job_state.MODE_REVIEW)
        self.assertEqual(result.status, pipeline_flow.STATUS_GATE)
        self.assertEqual(result.gate, "final_confirm")
        self.assertIn("render", runner.stages)

    def test_upload_only_happens_after_the_final_confirmation(self):
        runner = FakeRunner()
        self.advance(runner, job_state.MODE_REVIEW)
        pipeline_flow.approve(self.work_dir)
        self.advance(runner, job_state.MODE_REVIEW)
        pipeline_flow.approve(self.work_dir)
        self.advance(runner, job_state.MODE_REVIEW)
        pipeline_flow.approve(self.work_dir)
        self.advance(runner, job_state.MODE_REVIEW)
        pipeline_flow.approve(self.work_dir)
        self.advance(runner, job_state.MODE_REVIEW)
        self.assertNotIn("upload", runner.stages)

        pipeline_flow.approve(self.work_dir)
        result = self.advance(runner, job_state.MODE_REVIEW)
        self.assertEqual(result.status, pipeline_flow.STATUS_DONE)
        # x_post (no gate) runs immediately after upload in the same pass.
        self.assertEqual(runner.stages[-2:], ["upload", "x_post"])

    def test_a_human_intervenes_exactly_five_times(self):
        runner = FakeRunner()
        gates = []
        result = self.advance(runner, job_state.MODE_REVIEW)
        while result.status == pipeline_flow.STATUS_GATE:
            gates.append(result.gate)
            pipeline_flow.approve(self.work_dir)
            result = self.advance(runner, job_state.MODE_REVIEW)
        self.assertEqual(
            gates,
            ["script_review", "x_thread_confirm", "x_photo_intake", "thumbnail_intake", "final_confirm"],
        )
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
            ["script_review", "x_thread_confirm", "x_photo_intake", "thumbnail_intake", "tts_review",
             "caption_review", "broll_review", "render_config", "final_confirm"],
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
        self.advance_past_x_photo_gate(runner)
        result = self.advance(runner, job_state.MODE_AUTO)
        self.assertEqual(result.status, pipeline_flow.STATUS_FAILED)
        self.assertEqual(result.stage, "tts")
        self.assertEqual(runner.stages.count("tts"), pipeline_flow.MAX_ATTEMPTS_PER_STAGE)

    def test_failure_stops_the_pipeline_rather_than_continuing(self):
        pipeline_flow.stage_guard.check = guard_failing("caption")
        runner = FakeRunner()
        self.advance_past_x_photo_gate(runner)
        self.advance(runner, job_state.MODE_AUTO)
        self.assertNotIn("broll", runner.stages)
        self.assertNotIn("upload", runner.stages)

    def test_failure_reason_is_persisted_for_the_operator(self):
        pipeline_flow.stage_guard.check = guard_failing("broll")
        runner = FakeRunner()
        self.advance_past_x_photo_gate(runner)
        self.advance(runner, job_state.MODE_AUTO)
        self.assertIn("broll 점검 실패", job_state.load(self.work_dir)["last_error"])

    def test_broll_retry_uses_the_targeted_command(self):
        pipeline_flow.stage_guard.check = guard_failing("broll")
        runner = FakeRunner()
        self.advance_past_x_photo_gate(runner)
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
        self.advance_past_x_photo_gate(runner)
        # Both AUTO-mode gates are already cleared, so tts's retry-then-recover
        # happens on the way straight through to done.
        result = self.advance(runner, job_state.MODE_AUTO)
        self.assertEqual(result.status, pipeline_flow.STATUS_DONE)
        self.assertEqual(runner.stages.count("tts"), 2)

    def test_a_crashing_command_is_not_retried(self):
        # Re-running an identical command that errored just burns time and API
        # budget; stop and let a human look.
        runner = FakeRunner(fail_stages=["caption"])
        self.advance_past_x_photo_gate(runner)
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
        self.assertEqual(result.gate, "x_thread_confirm")
        self.assertNotIn("script", later_runner.stages)

    def test_mode_defaults_to_the_stored_one(self):
        job_state.set_mode(self.work_dir, job_state.MODE_AUTO)
        result = pipeline_flow.advance(self.work_dir, BASE_DIR, FakeRunner())
        self.assertEqual(result.status, pipeline_flow.STATUS_GATE)
        self.assertEqual(result.gate, "x_photo_intake")

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
