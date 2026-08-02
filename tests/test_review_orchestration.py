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
import pipeline_orchestrator as po


class FakeBot:
    """Stands in for telegram_bot/slack_bot: only the primitives ctx needs."""

    def __init__(self, work_root, fail_stages=()):
        self.BASE_DIR = Path("/base")
        self._work_root = Path(work_root)
        self.messages = []
        self.gates = []
        self.commands = []
        self.fail_stages = set(fail_stages)
        self.render_progress_started = False

    def work_dir(self, job_id):
        return self._work_root / job_id

    def send_message(self, chat_id, text):
        self.messages.append(text)

    def send_gate(self, chat_id, job, gate):
        self.gates.append(gate)

    def start_render_progress(self, chat_id, job_id, stop_event):
        self.render_progress_started = True
        return None

    def run_command(self, argv, job_id, topic=None, extra_env=None):
        self.commands.append((argv, extra_env or {}))
        name = Path(argv[0]).name
        if any(stage in name for stage in self.fail_stages):
            raise RuntimeError(f"{name} 실패")

    @property
    def scripts_run(self):
        return [Path(argv[0]).name for argv, _ in self.commands]


def guard_always_ok(stage, work_dir, settings=None):
    return True, ""


class ReviewOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.bot = FakeBot(self._tmp.name)
        self.job = {"job_id": "J1", "topic": "텃밭 가꾸기와 치매"}
        self._real_check = pipeline_flow.stage_guard.check
        pipeline_flow.stage_guard.check = guard_always_ok

    def tearDown(self):
        pipeline_flow.stage_guard.check = self._real_check
        self._tmp.cleanup()

    def work_dir(self):
        return self.bot.work_dir("J1")

    def test_first_run_stops_at_the_script_gate(self):
        result = po.run_review_pipeline(self.bot, 1, self.job)
        self.assertEqual(result.gate, "script_review")
        self.assertEqual(self.bot.gates, ["script_review"])
        self.assertEqual(self.job["stage"], "await_script_approval")
        self.assertEqual(self.bot.scripts_run, ["0_script.sh"])

    def test_script_gate_reuses_the_existing_approval_stage_token(self):
        # So the script gate's edit buttons keep working untouched.
        po.run_review_pipeline(self.bot, 1, self.job)
        self.assertEqual(po.GATE_STAGES["script_review"], "await_script_approval")
        self.assertEqual(self.job["stage"], "await_script_approval")

    def test_topic_is_passed_to_the_script_stage_only(self):
        po.run_review_pipeline(self.bot, 1, self.job)
        argv, _ = self.bot.commands[0]
        self.assertEqual(argv[-1], "텃밭 가꾸기와 치매")

        po.approve_review_gate(self.bot, 1, self.job)
        tts_argv = [argv for argv, _ in self.bot.commands if "1_tts.sh" in argv[0]][0]
        self.assertNotIn("텃밭 가꾸기와 치매", tts_argv)

    def test_topic_json_takes_precedence_over_free_text(self):
        self.job["topic_json"] = "/work/topic_plan.json"
        po.run_review_pipeline(self.bot, 1, self.job)
        argv, _ = self.bot.commands[0]
        self.assertEqual(argv[-2:], ["--topic-json", "/work/topic_plan.json"])

    def test_approval_runs_to_the_final_gate_without_uploading(self):
        po.run_review_pipeline(self.bot, 1, self.job)
        result = po.approve_review_gate(self.bot, 1, self.job)
        self.assertEqual(result.gate, "final_confirm")
        self.assertEqual(self.job["stage"], "await_final_confirm")
        self.assertNotIn("3_upload.sh", self.bot.scripts_run)

    def test_second_approval_uploads_and_finishes(self):
        po.run_review_pipeline(self.bot, 1, self.job)
        po.approve_review_gate(self.bot, 1, self.job)
        result = po.approve_review_gate(self.bot, 1, self.job)
        self.assertEqual(result.status, pipeline_flow.STATUS_DONE)
        self.assertEqual(self.job["stage"], "done")
        # x_post (no gate of its own) runs immediately after upload.
        self.assertEqual(self.bot.scripts_run[-2:], ["3_upload.sh", "x_post.sh"])

    def test_the_human_sees_exactly_two_gates(self):
        po.run_review_pipeline(self.bot, 1, self.job)
        po.approve_review_gate(self.bot, 1, self.job)
        po.approve_review_gate(self.bot, 1, self.job)
        self.assertEqual(self.bot.gates, ["script_review", "final_confirm"])

    def test_failure_reports_the_stage_and_reason(self):
        self.bot.fail_stages = {"1_caption.sh"}
        po.run_review_pipeline(self.bot, 1, self.job)
        result = po.approve_review_gate(self.bot, 1, self.job)
        self.assertEqual(result.status, pipeline_flow.STATUS_FAILED)
        self.assertEqual(self.job["stage"], "failed")
        self.assertTrue(any("caption" in m for m in self.bot.messages))

    def test_switch_to_review_continues_a_job_started_elsewhere(self):
        # A /run job whose script is already approved opts into two-gate mode.
        job = {"job_id": "J2", "topic": "수면과 기억력"}
        bot = FakeBot(self._tmp.name)
        result = po.switch_to_review(bot, 1, job)
        self.assertEqual(result.gate, "final_confirm")
        self.assertNotIn("0_script.sh", bot.scripts_run)

    def test_mode_is_persisted_so_another_process_can_resume(self):
        po.run_review_pipeline(self.bot, 1, self.job)
        state = job_state.load(self.work_dir())
        self.assertEqual(state["mode"], job_state.MODE_REVIEW)
        self.assertEqual(state["awaiting"], "script_review")
        self.assertEqual(state["settings"]["topic"], "텃밭 가꾸기와 치매")

    def test_render_reports_progress_rather_than_going_silent(self):
        # A 1080p render is long; no ticks reads as a hang.
        po.run_review_pipeline(self.bot, 1, self.job)
        po.approve_review_gate(self.bot, 1, self.job)
        self.assertTrue(self.bot.render_progress_started)
        self.assertIn("2_render.sh", self.bot.scripts_run)

    def test_cancellation_ends_the_run_instead_of_retrying(self):
        class Cancelled(RuntimeError):
            pass

        bot = FakeBot(self._tmp.name, fail_stages={"1_tts.sh"})
        bot.PASSTHROUGH_EXCEPTIONS = (Cancelled,)

        original = bot.run_command

        def cancelling(argv, job_id, topic=None, extra_env=None):
            if "1_tts.sh" in argv[0]:
                bot.commands.append((argv, extra_env or {}))
                raise Cancelled("사용자가 취소했습니다.")
            return original(argv, job_id, topic, extra_env)

        bot.run_command = cancelling
        job = {"job_id": "J3", "topic": "취소 테스트"}
        po.run_review_pipeline(bot, 1, job)
        with self.assertRaises(Cancelled):
            po.approve_review_gate(bot, 1, job)
        # Retrying is exactly what the operator asked to stop.
        self.assertEqual(bot.scripts_run.count("1_tts.sh"), 1)

    def test_render_settings_reach_the_stage_environment(self):
        self.job["caption_font_size"] = 72
        po.run_review_pipeline(self.bot, 1, self.job)
        _, env = self.bot.commands[0]
        self.assertEqual(env["CAPTION_FONT_SIZE"], "72")


if __name__ == "__main__":
    unittest.main()
