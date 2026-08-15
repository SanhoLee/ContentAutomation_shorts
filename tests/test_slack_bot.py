import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dev" / "src" / "common"
sys.path.insert(0, str(SRC))
# dev/config.sh puts common/, youtube/ and instagram/ all on PYTHONPATH so
# the pipeline modules can import each other; tests must do the same.
sys.path.insert(0, str(ROOT / "dev" / "src" / "youtube"))
import job_state
import slack_bot


class SlackBotTests(unittest.TestCase):
    def test_ai_model_settings_screen_uses_environment_default(self):
        state = {"chats": {"C1": {}}}
        screens = []
        old_send_action = slack_bot.send_action_message
        old_save_state = slack_bot.save_state
        try:
            slack_bot.send_action_message = lambda channel_id, text, rows: screens.append((text, rows))
            slack_bot.save_state = lambda state: None
            result = slack_bot.handle_callback(
                state,
                {"message": {"chat": {"id": "C1"}}, "data": "cfg:cat:models"},
            )
        finally:
            slack_bot.send_action_message = old_send_action
            slack_bot.save_state = old_save_state
        self.assertTrue(result)
        self.assertTrue(screens)
        self.assertIn("AI 모델", screens[-1][0])
        self.assertIn("스크립트 모델", screens[-1][0])
        self.assertNotIn("env_value", state["chats"]["C1"].get("last_error", ""))
        self.assertEqual(slack_bot.action_request_label("cfg:cat:models"), "AI 모델 열기")

    def test_button_dispatch_announces_request_and_logs_result(self):
        notices = []
        original_state = slack_bot._STATE
        old_allow = slack_bot._allow
        old_send_message = slack_bot.send_message
        old_handle_callback = slack_bot.handle_callback
        old_save_state = slack_bot.save_state
        try:
            slack_bot._STATE = {"chats": {}}
            slack_bot._allow = lambda channel_id, user_id: True
            slack_bot.send_message = lambda channel_id, text: notices.append(text)
            slack_bot.handle_callback = lambda state, callback: True
            slack_bot.save_state = lambda state: None
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                slack_bot._dispatch_action({
                    "channel": {"id": "C1"},
                    "user": {"id": "U1"},
                    "message": {"ts": "123.456"},
                    "actions": [{"value": "show_status"}],
                })
        finally:
            slack_bot._STATE = original_state
            slack_bot._allow = old_allow
            slack_bot.send_message = old_send_message
            slack_bot.handle_callback = old_handle_callback
            slack_bot.save_state = old_save_state
        self.assertIn("요청됨: 현재 작업 상태 확인", notices)
        self.assertIn("slack_action_requested", output.getvalue())
        self.assertIn("slack_action_finished", output.getvalue())
        self.assertIn('result="handled"', output.getvalue())

    def test_button_error_screen_has_safe_recovery_navigation(self):
        screens = []
        old_send_action = slack_bot.send_action_message
        try:
            slack_bot.send_action_message = lambda channel_id, text, rows: screens.append((text, rows))
            slack_bot._send_recovery_error("C1", "open_settings", RuntimeError("설정 오류"))
        finally:
            slack_bot.send_action_message = old_send_action
        text, rows = screens[-1]
        self.assertIn("요청 처리 실패", text)
        callbacks = {item["callback_data"] for row in rows for item in row}
        self.assertEqual(callbacks, {"show_home", "open_settings", "show_status"})

    def test_home_screen_exposes_safe_content_entry_points(self):
        expected = {
            "start_content:review", "start_content:auto", "start_content:trend",
            "show_status", "open_settings", "show_home",
        }
        callbacks = {
            item["callback_data"]
            for row in slack_bot.home_button_rows()
            for item in row
        }
        self.assertTrue(expected.issubset(callbacks))
        text = slack_bot.home_screen_text({})
        self.assertIn("Brain50 콘텐츠 제작 홈", text)
        self.assertIn("주제를 입력하고 실행 전 확인", text)

    def test_start_button_waits_for_topic_and_confirmation(self):
        state = {"chats": {"C1": {}}}
        screens, started = [], []
        old_send_action = slack_bot.send_action_message
        old_start_background = slack_bot.start_background_task
        old_save_state = slack_bot.save_state
        try:
            slack_bot.send_action_message = lambda channel_id, text, rows: screens.append((text, rows))
            slack_bot.start_background_task = lambda *args: started.append(args)
            slack_bot.save_state = lambda state: None

            callback = {"message": {"chat": {"id": "C1"}}, "data": "start_content:review"}
            slack_bot.handle_callback(state, callback)
            job = state["chats"]["C1"]
            self.assertEqual(job["start_draft"], {"mode": "review"})
            self.assertFalse(started)
            self.assertIn("1/2 주제 입력", screens[-1][0])

            slack_bot.handle_message(state, {"chat": {"id": "C1"}, "text": "오메가3와 기억력"})
            self.assertEqual(job["start_draft"]["topic"], "오메가3와 기억력")
            self.assertFalse(started)
            self.assertIn("2/2 실행 확인", screens[-1][0])
            confirm_callbacks = {item["callback_data"] for row in screens[-1][1] for item in row}
            self.assertIn("start_confirm:review", confirm_callbacks)
            self.assertIn("start_reenter_topic", confirm_callbacks)
            self.assertIn("start_cancel", confirm_callbacks)

            callback["data"] = "start_confirm:review"
            slack_bot.handle_callback(state, callback)
            self.assertTrue(started)
        finally:
            slack_bot.send_action_message = old_send_action
            slack_bot.start_background_task = old_start_background
            slack_bot.save_state = old_save_state

    def test_dev_goal_button_collects_objective_seed_and_confirms_before_start(self):
        state = {"chats": {"C1": {"job_id": "existing", "stage": "await_script_approval"}}}
        screens, started, commands = [], [], []
        old_send_action = slack_bot.send_action_message
        old_start_background = slack_bot.start_background_task
        old_handle_run_goal = slack_bot.handle_run_goal
        old_save_state = slack_bot.save_state
        try:
            slack_bot.send_action_message = lambda channel_id, text, rows: screens.append((text, rows))
            slack_bot.start_background_task = lambda *args: started.append(args)
            slack_bot.handle_run_goal = lambda channel_id, job, command: commands.append(command)
            slack_bot.save_state = lambda state: None
            callback = {"message": {"chat": {"id": "C1"}}, "data": "start_goal"}

            slack_bot.handle_callback(state, callback)
            job = state["chats"]["C1"]
            self.assertEqual(job["job_id"], "existing")
            self.assertEqual(job["goal_draft"], {})
            self.assertIn("1/3 목표 선택", screens[-1][0])
            self.assertFalse(started)

            callback["data"] = "goal:objective:subscriber_growth"
            slack_bot.handle_callback(state, callback)
            self.assertEqual(job["goal_draft"]["objective"], "subscriber_growth")
            self.assertIn("2/3 씨드 선택", screens[-1][0])

            callback["data"] = "goal:seed:input"
            slack_bot.handle_callback(state, callback)
            self.assertTrue(job["goal_draft"]["awaiting_seed"])
            self.assertIn("2/3 씨드 입력", screens[-1][0])

            slack_bot.handle_message(state, {"chat": {"id": "C1"}, "text": "수면"})
            self.assertEqual(job["goal_draft"]["seed"], "수면")
            self.assertNotIn("awaiting_seed", job["goal_draft"])
            self.assertIn("3/3 실행 확인", screens[-1][0])
            self.assertIn("씨드: 수면", screens[-1][0])
            self.assertEqual(job["job_id"], "existing")
            self.assertFalse(started)

            callback["data"] = "goal:confirm"
            slack_bot.handle_callback(state, callback)
            self.assertNotIn("goal_draft", job)
            self.assertEqual(len(started), 1)
            started[0][-1]()
            self.assertEqual(commands, ["/run_goal subscriber_growth 수면"])
        finally:
            slack_bot.send_action_message = old_send_action
            slack_bot.start_background_task = old_start_background
            slack_bot.handle_run_goal = old_handle_run_goal
            slack_bot.save_state = old_save_state

    def test_dev_goal_button_supports_channel_data_only_selection(self):
        state = {"chats": {"C1": {}}}
        screens, started, commands = [], [], []
        old_send_action = slack_bot.send_action_message
        old_start_background = slack_bot.start_background_task
        old_handle_run_goal = slack_bot.handle_run_goal
        old_save_state = slack_bot.save_state
        try:
            slack_bot.send_action_message = lambda channel_id, text, rows: screens.append((text, rows))
            slack_bot.start_background_task = lambda *args: started.append(args)
            slack_bot.handle_run_goal = lambda channel_id, job, command: commands.append(command)
            slack_bot.save_state = lambda state: None
            callback = {"message": {"chat": {"id": "C1"}}, "data": "start_goal"}
            slack_bot.handle_callback(state, callback)
            callback["data"] = "goal:objective:balanced"
            slack_bot.handle_callback(state, callback)
            callback["data"] = "goal:seed:none"
            slack_bot.handle_callback(state, callback)

            self.assertIn("채널 데이터 기반 자동 선정", screens[-1][0])
            callback["data"] = "goal:confirm"
            slack_bot.handle_callback(state, callback)
            started[0][-1]()
            self.assertEqual(commands, ["/run_goal balanced"])
        finally:
            slack_bot.send_action_message = old_send_action
            slack_bot.start_background_task = old_start_background
            slack_bot.handle_run_goal = old_handle_run_goal
            slack_bot.save_state = old_save_state

    def test_dev_home_exposes_goal_planning_button(self):
        callbacks = {
            item["callback_data"]
            for row in slack_bot.home_button_rows()
            for item in row
        }
        self.assertIn("start_goal", callbacks)
        self.assertEqual(slack_bot.action_request_label("start_goal"), "목표 기반 자동 기획 열기")

    def test_invalid_goal_does_not_replace_existing_slack_job(self):
        job = {"job_id": "existing", "stage": "await_script_approval", "topic": "기존 주제"}
        messages = []
        old_send_message = slack_bot.send_message
        old_run_command = slack_bot.run_command
        try:
            slack_bot.send_message = lambda channel_id, text: messages.append(text)
            slack_bot.run_command = lambda *args, **kwargs: self.fail("invalid goal must not run")
            slack_bot.handle_run_goal("C1", job, "/run_goal invalid_goal 수면")
        finally:
            slack_bot.send_message = old_send_message
            slack_bot.run_command = old_run_command
        self.assertEqual(job, {"job_id": "existing", "stage": "await_script_approval", "topic": "기존 주제"})
        self.assertIn("목표 입력 오류", messages[-1])

    def test_run_slash_commands_also_require_confirmation(self):
        commands = {
            "/run 주제 A": "review",
            "/run_auto 주제 B": "auto",
            "/trend 주제 C": "trend",
        }
        for text, mode in commands.items():
            state = {"chats": {"C1": {}}}
            screens, started = [], []
            old_send_action = slack_bot.send_action_message
            old_start_background = slack_bot.start_background_task
            try:
                slack_bot.send_action_message = lambda channel_id, body, rows: screens.append(body)
                slack_bot.start_background_task = lambda *args: started.append(args)
                slack_bot.handle_message(state, {"chat": {"id": "C1"}, "text": text})
            finally:
                slack_bot.send_action_message = old_send_action
                slack_bot.start_background_task = old_start_background
            draft = state["chats"]["C1"]["start_draft"]
            self.assertEqual(draft["mode"], mode)
            self.assertTrue(draft["topic"].startswith("주제"))
            self.assertFalse(started)
            self.assertIn("2/2 실행 확인", screens[-1])

    def test_start_flow_back_preserves_existing_job(self):
        job = {"job_id": "existing", "topic": "기존 주제", "stage": "await_caption_approval"}
        state = {"chats": {"C1": job}}
        homes = []
        old_send_action = slack_bot.send_action_message
        old_send_home = slack_bot.send_home_screen
        old_save_state = slack_bot.save_state
        try:
            slack_bot.send_action_message = lambda *args, **kwargs: None
            slack_bot.send_home_screen = lambda *args, **kwargs: homes.append(args)
            slack_bot.save_state = lambda state: None
            slack_bot.handle_callback(state, {"message": {"chat": {"id": "C1"}}, "data": "start_content:auto"})
            slack_bot.handle_callback(state, {"message": {"chat": {"id": "C1"}}, "data": "start_cancel"})
        finally:
            slack_bot.send_action_message = old_send_action
            slack_bot.send_home_screen = old_send_home
            slack_bot.save_state = old_save_state
        self.assertNotIn("start_draft", job)
        self.assertEqual(job["job_id"], "existing")
        self.assertEqual(job["stage"], "await_caption_approval")
        self.assertTrue(homes)

    def test_confirmed_start_uses_the_selected_mode(self):
        calls = []
        old_start_background = slack_bot.start_background_task
        old_handle_run = slack_bot.handle_run
        old_handle_run_auto = slack_bot.handle_run_auto
        try:
            slack_bot.start_background_task = lambda state, chat_id, job, label, target: target()
            slack_bot.handle_run = lambda chat_id, job, text, trend=False: calls.append(("trend" if trend else "review", text))
            slack_bot.handle_run_auto = lambda chat_id, job, text: calls.append(("auto", text))
            for mode in ("review", "auto", "trend"):
                job = {"start_draft": {"mode": mode, "topic": "테스트 주제"}}
                slack_bot.confirm_start_flow({"chats": {"C1": job}}, "C1", job, mode)
        finally:
            slack_bot.start_background_task = old_start_background
            slack_bot.handle_run = old_handle_run
            slack_bot.handle_run_auto = old_handle_run_auto
        self.assertEqual(calls, [
            ("review", "/run 테스트 주제"),
            ("auto", "/run_auto 테스트 주제"),
            ("trend", "/trend 테스트 주제"),
        ])

    def test_startup_posts_a_top_level_home_when_channel_is_configured(self):
        old_channel, old_state = slack_bot.ALLOWED_CHANNEL_ID, slack_bot._STATE
        old_send_home, old_save_state = slack_bot.send_home_screen, slack_bot.save_state
        calls = []
        try:
            slack_bot.ALLOWED_CHANNEL_ID = "C1"
            slack_bot._STATE = {"chats": {"C1": {"slack_thread_ts": "old-thread"}}}
            slack_bot.send_home_screen = lambda *args, **kwargs: calls.append((args, kwargs))
            slack_bot.save_state = lambda state: None
            self.assertTrue(slack_bot.announce_startup_home())
        finally:
            slack_bot.ALLOWED_CHANNEL_ID = old_channel
            slack_bot._STATE = old_state
            slack_bot.send_home_screen = old_send_home
            slack_bot.save_state = old_save_state
        self.assertTrue(calls)
        self.assertTrue(calls[0][1]["top_level"])

    def test_app_home_publishes_the_same_entry_points(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def views_publish(self, **kwargs):
                self.calls.append(kwargs)

        old_channel, old_state = slack_bot.ALLOWED_CHANNEL_ID, slack_bot._STATE
        client = FakeClient()
        try:
            slack_bot.ALLOWED_CHANNEL_ID = "C1"
            slack_bot._STATE = {"chats": {"C1": {}}}
            slack_bot.publish_home("U1", client=client)
        finally:
            slack_bot.ALLOWED_CHANNEL_ID = old_channel
            slack_bot._STATE = old_state
        self.assertEqual(client.calls[0]["user_id"], "U1")
        view = client.calls[0]["view"]
        self.assertEqual(view["type"], "home")
        values = {
            element["value"]
            for block in view["blocks"] if block.get("type") == "actions"
            for element in block["elements"]
        }
        self.assertIn("start_content:review", values)
        self.assertIn("start_content:auto", values)

    def test_block_buttons_keep_workflow_callback_data(self):
        blocks = slack_bot._blocks("승인", [[{"text": "승인", "callback_data": "approve:await_script_approval"}]])
        button = blocks[1]["elements"][0]
        self.assertEqual(button["action_id"], "workflow_action_0_0")
        self.assertEqual(button["value"], "approve:await_script_approval")

    def test_every_button_action_id_is_unique_in_a_home_view(self):
        blocks = slack_bot._blocks(slack_bot.home_screen_text({}), slack_bot.home_button_rows())
        action_ids = [
            element["action_id"]
            for block in blocks if block.get("type") == "actions"
            for element in block["elements"]
        ]
        self.assertEqual(len(action_ids), len(set(action_ids)))
        self.assertTrue(all(slack_bot.re.fullmatch(r"workflow_action_\d+_\d+", action_id) for action_id in action_ids))

    def test_event_conversion_preserves_text_and_file(self):
        event = {"channel": "C1", "text": "/run 테스트", "files": [{"url_private": "https://example.invalid/a"}]}
        message = slack_bot._event_to_message(event)
        self.assertEqual(message["chat"]["id"], "C1")
        self.assertEqual(message["text"], "/run 테스트")
        self.assertIn("document", message)

    def test_slack_source_does_not_import_or_reference_telegram_bot(self):
        # Telegram was removed as a driver; nothing may reintroduce it here.
        source = (ROOT / "dev" / "src" / "common" / "slack_bot.py").read_text(encoding="utf-8")
        self.assertNotIn("import telegram_bot", source)
        self.assertNotIn("from telegram_bot", source)
        self.assertNotIn("api.telegram.org", source)

    def test_slack_settings_are_managed_by_the_standalone_workflow(self):
        job, messages = {}, []
        old_send_message = slack_bot.send_message
        try:
            slack_bot.send_message = lambda channel_id, text: messages.append(text)
            slack_bot.handle_set("C1", job, "/set pace=fast duration=75")
        finally:
            slack_bot.send_message = old_send_message
        self.assertEqual(job["speech_pace"], "fast")
        self.assertEqual(job["target_duration_sec"], "75")
        self.assertEqual(slack_bot._build_extra_env(job)["ATEMPO"], "1.2")

    def test_access_restrictions_can_require_channel_and_user(self):
        old_channel, old_user = slack_bot.ALLOWED_CHANNEL_ID, slack_bot.ALLOWED_USER_ID
        try:
            slack_bot.ALLOWED_CHANNEL_ID, slack_bot.ALLOWED_USER_ID = "C1", "U1"
            self.assertTrue(slack_bot._allow("C1", "U1"))
            self.assertFalse(slack_bot._allow("C2", "U1"))
            self.assertFalse(slack_bot._allow("C1", "U2"))
        finally:
            slack_bot.ALLOWED_CHANNEL_ID, slack_bot.ALLOWED_USER_ID = old_channel, old_user

    def test_job_ids_are_unique_for_concurrent_channels(self):
        job_ids = {slack_bot.new_job_id() for _ in range(100)}
        self.assertEqual(len(job_ids), 100)
        self.assertTrue(all(job_id.startswith("slack_") for job_id in job_ids))

    def test_registers_documented_set_commands(self):
        command_names = {name for name, _ in slack_bot.command_specs()}
        self.assertIn("set", command_names)
        self.assertIn("set_all", command_names)

        job = {}
        messages = []
        old_send_message = slack_bot.send_message
        try:
            slack_bot.send_message = lambda channel_id, text: messages.append(text)
            slack_bot.handle_set("C1", job, "/set font_size=64 web=off")
        finally:
            slack_bot.send_message = old_send_message
        self.assertEqual(job["caption_font_size"], "64")
        self.assertFalse(job["web_research"])

    def test_slack_uses_non_reserved_app_status_command(self):
        command_names = {name for name, _ in slack_bot.command_specs()}
        self.assertIn("app_status", command_names)
        self.assertNotIn("status", command_names)
        self.assertIn("/app_status", slack_bot.help_text())

        state = {"chats": {"C1": {"busy": "렌더링", "stage": "await_render_approval"}}}
        messages = []
        old_send_message, old_send_action_message = slack_bot.send_message, slack_bot.send_action_message
        try:
            slack_bot.send_message = lambda channel_id, text: messages.append(text)
            slack_bot.send_action_message = lambda channel_id, text, rows: messages.append(text)
            slack_bot.handle_message(state, {"chat": {"id": "C1"}, "text": "/app_status"})
        finally:
            slack_bot.send_message = old_send_message
            slack_bot.send_action_message = old_send_action_message
        self.assertTrue(any("최종 영상 확인" in message for message in messages))

    def test_every_review_stage_has_safe_navigation(self):
        for index, stage in enumerate(slack_bot.WORKFLOW_STAGES):
            callbacks = {
                item["callback_data"]
                for row in slack_bot.approval_buttons(stage)
                for item in row
            }
            self.assertIn(f"approve:{stage}", callbacks)
            self.assertIn(f"auto_finish:{stage}", callbacks)
            self.assertIn("show_status", callbacks)
            self.assertIn("cancel_all", callbacks)
            if index:
                self.assertIn(f"back:{stage}:{slack_bot.WORKFLOW_STAGES[index - 1]}", callbacks)

    def test_progress_card_summarizes_current_stage(self):
        text = slack_bot.workflow_status_text({
            "job_id": "job-123",
            "topic": "오메가3와 기억력",
            "stage": "await_caption_approval",
        })
        self.assertIn("3/7 자막 확인", text)
        self.assertIn("오메가3와 기억력", text)
        self.assertIn("job-123", text)

    def test_forward_moves_to_the_next_review_stage(self):
        expected_next = {
            "await_script_approval": "await_tts_approval",
            "await_tts_approval": "await_caption_approval",
            "await_caption_approval": "await_broll_approval",
            "await_broll_approval": "await_render_config",
            "await_render_config": "await_thumbnail_intake",
            "await_thumbnail_intake": "await_render_approval",
            "await_render_approval": "await_upload_meta_approval",
            "await_upload_meta_approval": "done",
        }
        sender_names = ("send_tts", "send_caption", "send_broll", "send_render_ready",
                         "send_rendered_video", "send_upload_meta", "send_message",
                         "send_thumbnail_prompt")
        originals = {name: getattr(slack_bot, name) for name in sender_names}
        old_run_command, old_run_render = slack_bot.run_command, slack_bot.run_render
        try:
            slack_bot.run_command = lambda *a, **k: None
            for name in sender_names:
                setattr(slack_bot, name, lambda *a, **k: None)
            slack_bot.run_render = lambda chat_id, job: job.update(stage="await_render_approval")
            for stage, next_stage in expected_next.items():
                job = {"job_id": f"job-{stage}", "topic": "테스트", "stage": stage}
                slack_bot.run_next_stage("C1", job)
                self.assertEqual(job["stage"], next_stage)
        finally:
            slack_bot.run_command = old_run_command
            slack_bot.run_render = old_run_render
            for name, original in originals.items():
                setattr(slack_bot, name, original)

    def test_auto_finish_can_resume_from_every_review_stage(self):
        # slack_bot drives this through pipeline_flow.STAGES, which includes
        # scene_visuals and x_thread (right after script) and x_post (right
        # after upload). thumbnail_intake now parks every one of these runs
        # once before render -- it's the one gate auto mode still honours --
        # so each expected command list stops there; approving it lets the
        # rest (render onward) run in a second pass. await_script_approval
        # resumes before x_thread has run, so it hits x_photo_intake first.
        expected_commands_before_gate = {
            "await_script_approval": ["scene_visuals.sh", "x_thread.sh", "1_tts.sh", "1_caption.sh", "1_broll.sh"],
            "await_tts_approval": ["1_caption.sh", "1_broll.sh"],
            "await_caption_approval": ["1_broll.sh"],
            "await_broll_approval": [],
            "await_render_config": [],
            "await_render_approval": [],
            "await_upload_meta_approval": [],
        }
        extra_gate_stages = {"await_script_approval": ["scene_visuals.sh", "x_thread.sh"]}
        # These three stages resume from render.sh (or later), so pipeline_flow
        # is already past broll and its thumbnail_intake gate never fires again.
        no_gate_stages = {"await_render_approval", "await_upload_meta_approval"}
        # This test is about which stages run, not about the stage checks --
        # those have their own coverage in test_stage_guard.py, and the fake
        # job directories here hold no artifacts for them to inspect.
        real_check = slack_bot.pipeline_flow.stage_guard.check
        slack_bot.pipeline_flow.stage_guard.check = lambda *a, **k: (True, "")
        try:
            self._assert_auto_finish_resumes(expected_commands_before_gate, no_gate_stages,
                                              extra_gate_stages)
        finally:
            slack_bot.pipeline_flow.stage_guard.check = real_check

    def _assert_auto_finish_resumes(self, expected_commands_before_gate, no_gate_stages,
                                     extra_gate_stages=None):
        extra_gate_stages = extra_gate_stages or {}
        for stage in slack_bot.WORKFLOW_STAGES:
            commands, renders, gates, x_photo_gates = [], [], [], []
            old_run_command = slack_bot.run_command
            old_run_render_silent = slack_bot._run_render_silent
            old_send_message = slack_bot.send_message
            old_send_thumbnail_prompt = slack_bot.send_thumbnail_prompt
            old_send_x_photo_prompt = slack_bot.send_x_photo_prompt
            try:
                slack_bot.run_command = lambda args, *a, **k: commands.append(Path(args[0]).name)
                slack_bot._run_render_silent = lambda *a, **k: renders.append("render")
                slack_bot.send_message = lambda *a, **k: None
                slack_bot.send_thumbnail_prompt = lambda chat_id, job_id: gates.append(job_id)
                slack_bot.send_x_photo_prompt = lambda chat_id, job_id: x_photo_gates.append(job_id)
                job = {"job_id": f"job-{stage}", "topic": "테스트", "stage": stage}
                slack_bot.run_remaining_to_upload("C1", job)

                if stage in extra_gate_stages:
                    # x_thread hasn't run yet at this resume point, so
                    # x_photo_intake fires before thumbnail_intake can.
                    self.assertEqual(x_photo_gates, [job["job_id"]])
                    self.assertEqual(job["stage"], "await_x_photo_intake")
                    self.assertEqual(commands, extra_gate_stages[stage])
                    slack_bot.approve_review_gate("C1", job)

                if stage in no_gate_stages:
                    self.assertEqual(gates, [])
                    self.assertEqual(job["stage"], "done")
                else:
                    self.assertEqual(gates, [job["job_id"]])
                    self.assertEqual(job["stage"], "await_thumbnail_intake")
                    self.assertEqual(commands, expected_commands_before_gate[stage])
                    self.assertEqual(renders, [])

                    # Production never re-enters run_to_completion here -- the
                    # real resume path is resume_after_thumbnail ->
                    # approve_review_gate -> run_review_pipeline. approve_review_gate
                    # calls pipeline_flow.approve() itself, so this must not
                    # approve again first -- a second approve() with nothing left
                    # awaiting would wipe the cleared_gate it just set.
                    slack_bot.approve_review_gate("C1", job)
                    self.assertEqual(job["stage"], "done")
                    self.assertEqual(renders, ["render"])
                    self.assertEqual(commands[-2:], ["3_upload.sh", "x_post.sh"])
            finally:
                slack_bot.run_command = old_run_command
                slack_bot._run_render_silent = old_run_render_silent
                slack_bot.send_message = old_send_message
                slack_bot.send_thumbnail_prompt = old_send_thumbnail_prompt
                slack_bot.send_x_photo_prompt = old_send_x_photo_prompt

    def test_cancel_request_is_available_while_busy(self):
        job_id = f"cancel-{slack_bot.__name__}"
        job = {"job_id": job_id, "stage": "running_after_review", "busy": "끝까지 자동 처리"}
        messages = []
        old_send_message = slack_bot.send_message
        try:
            slack_bot.send_message = lambda channel_id, text: messages.append(text)
            slack_bot.request_workflow_cancel("C1", job)
            self.assertTrue(job["cancel_requested"])
            with self.assertRaises(slack_bot.WorkflowCancelled):
                slack_bot.run_command(["unused"], job_id)
        finally:
            slack_bot.CANCELLED_JOB_IDS.discard(job_id)
            slack_bot.send_message = old_send_message
        self.assertTrue(any("취소 요청" in message for message in messages))

    def test_destructive_buttons_require_confirmation(self):
        state = {"chats": {"C1": {"job_id": "job-1", "topic": "테스트", "stage": "await_caption_approval"}}}
        confirmations, started = [], []
        old_send_action_message = slack_bot.send_action_message
        old_start_background_task = slack_bot.start_background_task
        old_save_state = slack_bot.save_state
        try:
            slack_bot.send_action_message = lambda channel_id, text, rows: confirmations.append(rows)
            slack_bot.start_background_task = lambda *args: started.append(args)
            slack_bot.save_state = lambda state: None
            callback = {"message": {"chat": {"id": "C1"}}, "data": "auto_finish:await_caption_approval"}
            slack_bot.handle_callback(state, callback)
            self.assertFalse(started)
            confirm_callbacks = {item["callback_data"] for row in confirmations[-1] for item in row}
            self.assertIn("auto_finish_confirm:await_caption_approval", confirm_callbacks)

            callback["data"] = "auto_finish_confirm:await_caption_approval"
            slack_bot.handle_callback(state, callback)
            self.assertTrue(started)

            confirmations.clear()
            callback["data"] = "cancel_all"
            slack_bot.handle_callback(state, callback)
            cancel_callbacks = {item["callback_data"] for row in confirmations[-1] for item in row}
            self.assertIn("cancel_confirm", cancel_callbacks)
        finally:
            slack_bot.send_action_message = old_send_action_message
            slack_bot.start_background_task = old_start_background_task
            slack_bot.save_state = old_save_state


class MaybePostXThreadTests(unittest.TestCase):
    """Auto-posting after upload must never mask the pipeline's own result.

    review/auto already run the x_post pipeline stage inside
    pipeline_flow.advance() before the job reaches "done", so this hook must
    check what's already on disk before calling post_thread() again --
    otherwise the "already posted" raise that guards against double-posting
    reads as an auto-post *failure* on every ordinary successful run.
    """

    def _patch(self, existing=None, post_result=None, post_error=None):
        calls = []
        old_load = slack_bot.x_thread_adapter.load_x_thread
        old_post = slack_bot.x_poster.post_thread
        old_send_message = slack_bot.send_message

        def fake_post(job_dir):
            calls.append(job_dir)
            if post_error is not None:
                raise post_error
            return post_result

        messages = []
        slack_bot.x_thread_adapter.load_x_thread = lambda job_dir: existing
        slack_bot.x_poster.post_thread = fake_post
        slack_bot.send_message = lambda channel_id, text: messages.append(text)
        return calls, messages, old_load, old_post, old_send_message

    def _restore(self, old_load, old_post, old_send_message):
        slack_bot.x_thread_adapter.load_x_thread = old_load
        slack_bot.x_poster.post_thread = old_post
        slack_bot.send_message = old_send_message

    def test_skips_when_job_not_done(self):
        calls, messages, old_load, old_post, old_send = self._patch()
        try:
            slack_bot._maybe_post_x_thread("C1", {"job_id": "J1", "stage": "await_render_approval"})
        finally:
            self._restore(old_load, old_post, old_send)
        self.assertEqual(calls, [])
        self.assertEqual(messages, [])

    def test_skips_when_no_job_id(self):
        calls, messages, old_load, old_post, old_send = self._patch()
        try:
            slack_bot._maybe_post_x_thread("C1", {"stage": "done"})
        finally:
            self._restore(old_load, old_post, old_send)
        self.assertEqual(calls, [])
        self.assertEqual(messages, [])

    def test_skips_silently_when_no_thread_was_built(self):
        """No x_thread.json (Claude budget guard, no evidence, etc.) is not a
        failure -- the x_thread stage is downstream-only and always exits 0."""
        calls, messages, old_load, old_post, old_send = self._patch(existing=None)
        try:
            slack_bot._maybe_post_x_thread("C1", {"job_id": "J1", "stage": "done"})
        finally:
            self._restore(old_load, old_post, old_send)
        self.assertEqual(calls, [])
        self.assertEqual(messages, [])

    def test_already_posted_by_pipeline_stage_reports_success_without_reposting(self):
        """This is the review/auto path: the pipeline's own x_post stage
        already posted the thread before the job reached "done". Calling
        post_thread() again would raise "already posted" -- that must not
        turn a successful run into a reported failure."""
        existing = {
            "posted": True,
            "tweet_ids": ["1", "2", "3"],
            "thread_url": "https://x.com/i/web/status/1",
        }
        calls, messages, old_load, old_post, old_send = self._patch(existing=existing)
        try:
            slack_bot._maybe_post_x_thread("C1", {"job_id": "J1", "stage": "done"})
        finally:
            self._restore(old_load, old_post, old_send)
        self.assertEqual(calls, [])
        self.assertIn("게시 완료", messages[-1])
        self.assertIn("https://x.com/i/web/status/1", messages[-1])
        self.assertIn("3개", messages[-1])

    def test_success_reports_thread_url_and_tweet_count(self):
        """This is the legacy full-gate path (run_next_stage): x_post never
        ran as a pipeline stage, so a built-but-unposted thread is actually
        posted here."""
        existing = {"tweets": [{"text": "a"}]}
        result = {"tweet_ids": ["1", "2", "3"], "thread_url": "https://x.com/i/web/status/1"}
        calls, messages, old_load, old_post, old_send = self._patch(existing=existing, post_result=result)
        try:
            slack_bot._maybe_post_x_thread("C1", {"job_id": "J1", "stage": "done"})
        finally:
            self._restore(old_load, old_post, old_send)
        self.assertEqual(len(calls), 1)
        self.assertIn("게시 완료", messages[-1])
        self.assertIn("https://x.com/i/web/status/1", messages[-1])
        self.assertIn("3개", messages[-1])

    def test_failure_is_reported_not_raised_and_points_at_x_post(self):
        existing = {"tweets": [{"text": "a"}]}
        calls, messages, old_load, old_post, old_send = self._patch(
            existing=existing, post_error=RuntimeError("2/4개 게시 후 실패: 429"),
        )
        try:
            slack_bot._maybe_post_x_thread("C1", {"job_id": "J1", "stage": "done"})
        finally:
            self._restore(old_load, old_post, old_send)
        self.assertIn("자동 게시 실패", messages[-1])
        self.assertIn("429", messages[-1])
        self.assertIn("/x_post", messages[-1])

    def test_unexpected_exception_does_not_escape(self):
        existing = {"tweets": [{"text": "a"}]}
        calls, messages, old_load, old_post, old_send = self._patch(
            existing=existing, post_error=ValueError("boom"),
        )
        try:
            slack_bot._maybe_post_x_thread("C1", {"job_id": "J1", "stage": "done"})
        finally:
            self._restore(old_load, old_post, old_send)
        self.assertIn("boom", messages[-1])


class XSourcesDmTests(unittest.TestCase):
    """The sources block is no longer a trailing tweet -- it is DM'd to the
    operator so it can be copy-pasted. It must go out exactly once even
    though several paths reach a posted thread, and a Slack failure must
    never escape into a job whose thread is already live."""

    def _patch(self, payload, *, dm_error=None):
        saved = []
        dms = []
        old_load = slack_bot.x_thread_adapter.load_x_thread
        old_save = slack_bot.x_thread_adapter.save_x_thread
        old_send_dm = slack_bot.send_dm

        def fake_dm(text, user_id=None):
            if dm_error is not None:
                raise dm_error
            dms.append(text)

        slack_bot.x_thread_adapter.load_x_thread = lambda job_dir: payload
        slack_bot.x_thread_adapter.save_x_thread = lambda job_dir, data: saved.append(data)
        slack_bot.send_dm = fake_dm
        return dms, saved, (old_load, old_save, old_send_dm)

    def _restore(self, originals):
        (slack_bot.x_thread_adapter.load_x_thread,
         slack_bot.x_thread_adapter.save_x_thread,
         slack_bot.send_dm) = originals

    def test_sends_sources_and_marks_them_sent(self):
        payload = {"sources_text": "출처\nNeurology 2022 https://pubmed.ncbi.nlm.nih.gov/1/"}
        dms, saved, originals = self._patch(payload)
        try:
            sent = slack_bot._maybe_send_x_sources_dm("/tmp/J1")
        finally:
            self._restore(originals)
        self.assertTrue(sent)
        self.assertIn("pubmed.ncbi.nlm.nih.gov/1", dms[0])
        self.assertIn(slack_bot.SOURCES_DM_HEADER, dms[0])
        self.assertTrue(saved[0]["sources_dm_sent"])

    def test_does_not_send_twice(self):
        payload = {"sources_text": "출처\n- 2019년 코호트 연구", "sources_dm_sent": True}
        dms, saved, originals = self._patch(payload)
        try:
            sent = slack_bot._maybe_send_x_sources_dm("/tmp/J1")
        finally:
            self._restore(originals)
        self.assertFalse(sent)
        self.assertEqual(dms, [])
        self.assertEqual(saved, [])

    def test_no_sources_means_no_dm(self):
        for payload in (None, {}, {"sources_text": "  "}):
            dms, saved, originals = self._patch(payload)
            try:
                sent = slack_bot._maybe_send_x_sources_dm("/tmp/J1")
            finally:
                self._restore(originals)
            self.assertFalse(sent)
            self.assertEqual(dms, [])

    def test_dm_failure_is_swallowed_and_not_marked_sent(self):
        payload = {"sources_text": "출처\n- 근거"}
        dms, saved, originals = self._patch(payload, dm_error=RuntimeError("no DM channel"))
        try:
            with contextlib.redirect_stderr(io.StringIO()) as err:
                sent = slack_bot._maybe_send_x_sources_dm("/tmp/J1")
        finally:
            self._restore(originals)
        self.assertFalse(sent)
        self.assertEqual(saved, [])
        self.assertIn("no DM channel", err.getvalue())

    def test_posting_path_triggers_the_sources_dm(self):
        payload = {
            "posted": True, "tweet_ids": ["1"], "thread_url": "https://x.com/i/web/status/1",
            "sources_text": "출처\n- 근거",
        }
        dms, saved, originals = self._patch(payload)
        old_send_message = slack_bot.send_message
        slack_bot.send_message = lambda channel_id, text: None
        try:
            slack_bot._maybe_post_x_thread("C1", {"job_id": "J1", "stage": "done"})
        finally:
            slack_bot.send_message = old_send_message
            self._restore(originals)
        self.assertEqual(len(dms), 1)


class XPhotoIntakeTests(unittest.TestCase):
    """The operator makes the lead image and hands it back over Slack. The
    intake must never trap them in a mode they cannot type out of, and must
    never let a bad attachment reach a job that is mid-flight."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.job_dir = Path(self._tmp.name)
        self.messages = []
        self.sent_files = []
        self.saved = []
        self._originals = (
            slack_bot.work_dir, slack_bot.send_message, slack_bot.send_file_or_path,
            slack_bot.download_slack_file, slack_bot.x_thread_adapter.load_x_thread,
            slack_bot.x_thread_adapter.save_x_thread,
            slack_bot.x_photo_card.normalize_thread_photo,
        )
        self.payload = {"tweets": [{"index": 1, "text": "첫 트윗입니다"}]}

        slack_bot.work_dir = lambda job_id: self.job_dir
        slack_bot.send_message = lambda chat_id, text: self.messages.append(text)
        slack_bot.send_file_or_path = lambda chat_id, path, caption=None, as_video=False: (
            self.sent_files.append(str(path))
        )
        slack_bot.download_slack_file = lambda doc, dest: Path(dest).write_bytes(b"png-bytes")
        slack_bot.x_thread_adapter.load_x_thread = lambda job_dir: self.payload
        slack_bot.x_thread_adapter.save_x_thread = lambda job_dir, data: self.saved.append(data)
        slack_bot.x_photo_card.normalize_thread_photo = lambda src, dest: Path(dest)

    def tearDown(self):
        (slack_bot.work_dir, slack_bot.send_message, slack_bot.send_file_or_path,
         slack_bot.download_slack_file, slack_bot.x_thread_adapter.load_x_thread,
         slack_bot.x_thread_adapter.save_x_thread,
         slack_bot.x_photo_card.normalize_thread_photo) = self._originals
        self._tmp.cleanup()

    def _armed_job(self):
        job = {"job_id": "J1"}
        slack_bot.request_x_photo("C1", job)
        return job

    def _image(self, name="card.png", size=1024):
        return {"id": "F1", "name": name, "size": size}

    def test_request_arms_and_shows_the_lead_tweet(self):
        job = self._armed_job()
        self.assertTrue(job["x_photo_target"].endswith(slack_bot.x_photo_card.PHOTO_FILENAME))
        self.assertIn("첫 트윗입니다", self.messages[-1])

    def test_request_is_skipped_for_an_already_posted_thread(self):
        self.payload = {"tweets": [{"text": "a"}], "posted": True}
        job = {"job_id": "J1"}
        self.assertFalse(slack_bot.request_x_photo("C1", job))
        self.assertNotIn("x_photo_target", job)

    def test_unarmed_upload_is_not_consumed(self):
        job = {"job_id": "J1"}
        consumed = slack_bot.apply_x_photo_message("C1", job, {"document": self._image()})
        self.assertFalse(consumed)
        self.assertEqual(self.saved, [])

    def test_text_while_armed_falls_through_to_commands(self):
        job = self._armed_job()
        self.assertFalse(slack_bot.apply_x_photo_message("C1", job, {"text": "/app_status"}))
        # Still armed: a typed command must not cancel the pending request.
        self.assertIn("x_photo_target", job)

    def test_image_is_saved_and_recorded_on_the_thread(self):
        job = self._armed_job()
        self.assertTrue(slack_bot.apply_x_photo_message("C1", job, {"document": self._image()}))
        self.assertEqual(len(self.saved), 1)
        self.assertTrue(self.saved[0]["photo_path"].endswith(slack_bot.x_photo_card.PHOTO_FILENAME))
        # x_poster holds the thread until this says a human supplied the image.
        self.assertEqual(self.saved[0]["photo_source"], slack_bot.x_poster.PHOTO_SOURCE_OPERATOR)
        self.assertNotIn("x_photo_target", job)
        self.assertTrue(self.sent_files)

    def test_image_arriving_after_the_upload_posts_the_held_thread(self):
        # The pipeline's x_post stage held the thread for want of an image;
        # the upload is the release trigger, otherwise nothing ever posts it.
        job = self._armed_job()
        job["stage"] = "done"
        posted = []
        original = slack_bot.x_poster.post_thread
        slack_bot.x_poster.post_thread = lambda job_dir, **kw: (
            posted.append(job_dir) or {"thread_url": "https://x.com/i/web/status/1", "tweet_ids": ["1"]}
        )
        try:
            slack_bot.apply_x_photo_message("C1", job, {"document": self._image()})
        finally:
            slack_bot.x_poster.post_thread = original
        self.assertEqual(len(posted), 1)
        self.assertIn("X 스레드 게시 완료", self.messages[-1])

    def test_image_arriving_mid_pipeline_does_not_post_early(self):
        job = self._armed_job()
        job["stage"] = "running"
        original = slack_bot.x_poster.post_thread
        slack_bot.x_poster.post_thread = lambda *a, **kw: self.fail("should not post yet")
        try:
            slack_bot.apply_x_photo_message("C1", job, {"document": self._image()})
        finally:
            slack_bot.x_poster.post_thread = original
        self.assertEqual(len(self.saved), 1)

    def test_wrong_file_type_is_rejected_and_stays_armed(self):
        job = self._armed_job()
        self.assertTrue(
            slack_bot.apply_x_photo_message("C1", job, {"document": self._image("notes.pdf")})
        )
        self.assertEqual(self.saved, [])
        self.assertIn("x_photo_target", job)
        self.assertIn("이미지 파일만", self.messages[-1])

    def test_oversized_image_is_rejected_and_stays_armed(self):
        job = self._armed_job()
        big = self._image(size=slack_bot.X_PHOTO_MAX_BYTES + 1)
        self.assertTrue(slack_bot.apply_x_photo_message("C1", job, {"document": big}))
        self.assertEqual(self.saved, [])
        self.assertIn("x_photo_target", job)

    def test_download_failure_is_reported_not_raised(self):
        job = self._armed_job()
        def boom(doc, dest):
            raise RuntimeError("slack download 500")
        slack_bot.download_slack_file = boom
        self.assertTrue(slack_bot.apply_x_photo_message("C1", job, {"document": self._image()}))
        self.assertEqual(self.saved, [])
        self.assertIn("slack download 500", self.messages[-1])
        self.assertIn("x_photo_target", job)

    def test_posted_thread_refuses_a_replacement(self):
        job = self._armed_job()
        self.payload = {"tweets": [{"text": "a"}], "posted": True}
        self.assertTrue(slack_bot.apply_x_photo_message("C1", job, {"document": self._image()}))
        self.assertEqual(self.saved, [])
        self.assertNotIn("x_photo_target", job)
        self.assertIn("이미 게시된", self.messages[-1])

    def _handle(self, job, message):
        state = {"chats": {"C1": job}}
        original = slack_bot.save_state
        slack_bot.save_state = lambda _state: None
        try:
            slack_bot.handle_message(state, message)
        finally:
            slack_bot.save_state = original

    def test_busy_job_still_takes_the_upload_it_asked_for(self):
        # request_x_photo fires off the x_thread stage, but on the auto path the
        # job stays busy through render and upload. The busy gate used to bounce
        # the attachment before the intake ever saw it, so the operator's image
        # was silently dropped while the bot claimed to be waiting for it.
        job = self._armed_job()
        job["busy"] = "끝까지 자동 처리"
        self._handle(job, {"chat": {"id": "C1"}, "document": self._image()})
        self.assertEqual(len(self.saved), 1)
        self.assertNotIn("x_photo_target", job)
        self.assertFalse([m for m in self.messages if "진행 중입니다" in m])

    def test_busy_job_still_bounces_plain_text(self):
        job = self._armed_job()
        job["busy"] = "끝까지 자동 처리"
        self._handle(job, {"chat": {"id": "C1"}, "text": "이거 언제 끝나?"})
        self.assertEqual(self.saved, [])
        self.assertIn("진행 중입니다", self.messages[-1])


class ThumbnailIntakeTests(unittest.TestCase):
    """Unlike the X lead photo, render is genuinely on hold here: the intake
    must reject a bad attachment while staying armed, and resuming must route
    to the right driver depending on whether the job carries a pipeline_flow
    `mode` (review/auto) or is the legacy mode-less /run chain."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.job_dir = Path(self._tmp.name)
        self.messages = []
        self.sent_files = []
        self.normalized = []
        self._originals = (
            slack_bot.work_dir, slack_bot.send_message, slack_bot.send_file_or_path,
            slack_bot.download_slack_file, slack_bot.x_photo_card.normalize_thread_photo,
            slack_bot.start_background_task,
        )
        slack_bot.work_dir = lambda job_id: self.job_dir
        slack_bot.send_message = lambda chat_id, text: self.messages.append(text)
        slack_bot.send_file_or_path = lambda chat_id, path, caption=None, as_video=False: (
            self.sent_files.append(str(path))
        )
        slack_bot.download_slack_file = lambda doc, dest: Path(dest).write_bytes(b"png-bytes")
        def fake_normalize(src, dest, size=None):
            self.normalized.append(size)
            return Path(dest)
        slack_bot.x_photo_card.normalize_thread_photo = fake_normalize
        # Run resume_after_thumbnail inline so tests can assert on its effect
        # without spinning up a real background thread.
        slack_bot.start_background_task = lambda state, chat_id, job, label, target: target()

    def tearDown(self):
        (slack_bot.work_dir, slack_bot.send_message, slack_bot.send_file_or_path,
         slack_bot.download_slack_file, slack_bot.x_photo_card.normalize_thread_photo,
         slack_bot.start_background_task) = self._originals
        self._tmp.cleanup()

    def _armed_job(self, **extra):
        job = {"job_id": "J1", "stage": "await_thumbnail_intake",
               "thumbnail_target": str(self.job_dir / "thumbnail.png")}
        job.update(extra)
        return job

    def _image(self, name="frame.png", size=1024):
        return {"id": "F1", "name": name, "size": size}

    def _handle_message(self, job, message):
        state = {"chats": {"C1": job}}
        old_save_state = slack_bot.save_state
        slack_bot.save_state = lambda _state: None
        try:
            slack_bot.handle_message(state, message)
        finally:
            slack_bot.save_state = old_save_state

    def test_unarmed_upload_is_not_consumed(self):
        job = {"job_id": "J1"}
        consumed = slack_bot.apply_thumbnail_message(None, "C1", job, {"document": self._image()})
        self.assertFalse(consumed)

    def test_wrong_file_type_is_rejected_and_stays_armed(self):
        job = self._armed_job()
        self.assertTrue(
            slack_bot.apply_thumbnail_message(None, "C1", job, {"document": self._image("notes.pdf")})
        )
        self.assertIn("thumbnail_target", job)
        self.assertIn("이미지 파일만", self.messages[-1])

    def test_oversized_image_is_rejected_and_stays_armed(self):
        job = self._armed_job()
        big = self._image(size=slack_bot.THUMBNAIL_MAX_BYTES + 1)
        self.assertTrue(slack_bot.apply_thumbnail_message(None, "C1", job, {"document": big}))
        self.assertIn("thumbnail_target", job)

    def test_download_failure_is_reported_not_raised(self):
        job = self._armed_job()
        def boom(doc, dest):
            raise RuntimeError("slack download 500")
        slack_bot.download_slack_file = boom
        self.assertTrue(slack_bot.apply_thumbnail_message(None, "C1", job, {"document": self._image()}))
        self.assertIn("slack download 500", self.messages[-1])
        self.assertIn("thumbnail_target", job)

    def test_image_is_normalized_to_the_shorts_frame_and_resumes_via_gate(self):
        job = self._armed_job(mode=job_state.MODE_REVIEW)
        resumed = []
        old_approve = slack_bot.approve_review_gate
        old_run_next = slack_bot.run_next_stage
        slack_bot.approve_review_gate = lambda chat_id, job: resumed.append(("gate", chat_id))
        slack_bot.run_next_stage = lambda chat_id, job: resumed.append(("legacy", chat_id))
        try:
            self.assertTrue(
                slack_bot.apply_thumbnail_message(None, "C1", job, {"document": self._image()})
            )
        finally:
            slack_bot.approve_review_gate = old_approve
            slack_bot.run_next_stage = old_run_next
        self.assertNotIn("thumbnail_target", job)
        self.assertEqual(self.normalized, [slack_bot.THUMBNAIL_SIZE])
        self.assertTrue(self.sent_files)
        self.assertEqual(resumed, [("gate", "C1")])

    def test_image_resumes_the_legacy_chain_when_the_job_has_no_mode(self):
        job = self._armed_job()  # no "mode" key: legacy /run driver
        resumed = []
        old_approve = slack_bot.approve_review_gate
        old_run_next = slack_bot.run_next_stage
        slack_bot.approve_review_gate = lambda chat_id, job: resumed.append(("gate", chat_id))
        slack_bot.run_next_stage = lambda chat_id, job: resumed.append(("legacy", chat_id))
        try:
            slack_bot.apply_thumbnail_message(None, "C1", job, {"document": self._image()})
        finally:
            slack_bot.approve_review_gate = old_approve
            slack_bot.run_next_stage = old_run_next
        self.assertEqual(resumed, [("legacy", "C1")])

    def test_yes_button_arms_the_target_without_approving_the_gate(self):
        state = {"chats": {"C1": {"job_id": "J1", "stage": "await_thumbnail_intake"}}}
        old_save_state = slack_bot.save_state
        slack_bot.save_state = lambda _state: None
        try:
            callback = {"message": {"chat": {"id": "C1"}}, "data": "thumbnail_yes:await_thumbnail_intake"}
            slack_bot.handle_callback(state, callback)
        finally:
            slack_bot.save_state = old_save_state
        job = state["chats"]["C1"]
        self.assertTrue(job["thumbnail_target"].endswith("thumbnail.png"))
        self.assertIn("첨부해", self.messages[-1])

    def _no_button_resumes(self, job):
        state = {"chats": {"C1": job}}
        resumed = []
        old_start = slack_bot.start_background_task
        old_save_state = slack_bot.save_state
        old_approve = slack_bot.approve_review_gate
        old_run_next = slack_bot.run_next_stage
        slack_bot.start_background_task = lambda state, chat_id, job, label, target: target()
        slack_bot.save_state = lambda _state: None
        slack_bot.approve_review_gate = lambda chat_id, job: resumed.append("gate")
        slack_bot.run_next_stage = lambda chat_id, job: resumed.append("legacy")
        try:
            callback = {"message": {"chat": {"id": "C1"}}, "data": "approve:await_thumbnail_intake"}
            slack_bot.handle_callback(state, callback)
        finally:
            slack_bot.start_background_task = old_start
            slack_bot.save_state = old_save_state
            slack_bot.approve_review_gate = old_approve
            slack_bot.run_next_stage = old_run_next
        return resumed

    def test_no_button_skips_via_the_gate_for_a_moded_job(self):
        job = {"job_id": "J1", "stage": "await_thumbnail_intake", "mode": job_state.MODE_REVIEW}
        self.assertEqual(self._no_button_resumes(job), ["gate"])

    def test_no_button_skips_via_the_legacy_chain_for_a_mode_less_job(self):
        job = {"job_id": "J1", "stage": "await_thumbnail_intake"}
        self.assertEqual(self._no_button_resumes(job), ["legacy"])

    def test_no_button_deletes_a_stale_thumbnail_from_an_earlier_render_pass(self):
        # A retried/rewound render can leave thumbnail.png on disk from a
        # prior yes answer; 2_render.sh only checks the file's presence, so
        # skipping now must remove it or the stale image gets spliced in
        # against this answer.
        stale = self.job_dir / "thumbnail.png"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_bytes(b"stale-png")
        job = {"job_id": "J1", "stage": "await_thumbnail_intake"}
        self._no_button_resumes(job)
        self.assertFalse(stale.exists())

    def test_message_dispatch_routes_uploads_to_the_thumbnail_intake(self):
        job = self._armed_job(mode=job_state.MODE_REVIEW)
        old_approve = slack_bot.approve_review_gate
        slack_bot.approve_review_gate = lambda chat_id, job: None
        try:
            self._handle_message(job, {"chat": {"id": "C1"}, "document": self._image()})
        finally:
            slack_bot.approve_review_gate = old_approve
        self.assertNotIn("thumbnail_target", job)
        self.assertTrue(self.sent_files)


CANDIDATES = [
    {"topic_id": "2026-08-02_01_치매-초기증상", "title_hint": "치매 초기증상 건망증 차이",
     "seed": "치매", "score": 88, "score_breakdown": {"niche_relevance": 30, "novelty_vs_history": 20}},
    {"topic_id": "2026-08-02_02_수면-기억력", "title_hint": "수면 부족과 기억력 저하",
     "seed": "수면", "score": 74, "score_breakdown": {"niche_relevance": 20, "evidence_potential": 20}},
    {"topic_id": "2026-08-02_03_뇌-식단", "title_hint": "뇌 건강 식단 정리",
     "seed": "식단", "score": 65, "score_breakdown": {"niche_relevance": 20, "safety_tone": 10}},
]


class TopicCandidateFlowTests(unittest.TestCase):
    """The /topics flow is queue-level: it must never touch job state."""

    def _patch(self, *, candidates=None, selection=None, select_result=None):
        pipeline = slack_bot.topic_candidate_pipeline
        saved = {
            "top": pipeline.top_candidates,
            "current": pipeline.current_selection,
            "select": pipeline.select_topic,
            "send_action": slack_bot.send_action_message,
            "send_message": slack_bot.send_message,
        }
        screens, messages, selected_ids = [], [], []

        pipeline.top_candidates = lambda n=3, base_dir=None: list(candidates or [])
        pipeline.current_selection = lambda base_dir=None: selection
        pipeline.select_topic = lambda topic_id, base_dir=None: (
            selected_ids.append(topic_id) or select_result
        )
        slack_bot.send_action_message = lambda channel_id, text, rows: screens.append((text, rows))
        slack_bot.send_message = lambda channel_id, text: messages.append(text)
        return saved, screens, messages, selected_ids

    def _restore(self, saved):
        pipeline = slack_bot.topic_candidate_pipeline
        pipeline.top_candidates = saved["top"]
        pipeline.current_selection = saved["current"]
        pipeline.select_topic = saved["select"]
        slack_bot.send_action_message = saved["send_action"]
        slack_bot.send_message = saved["send_message"]

    @staticmethod
    def _callbacks(rows):
        return [item["callback_data"] for row in rows for item in row]

    def test_start_button_is_on_home_and_routes_to_refresh(self):
        """The home screen's "주제 선정 시작" button is the direct entry point
        for testing the queue-level flow -- trend collection then scoring --
        without a job in progress, reusing topic:refresh's own handler."""
        callbacks = {item["callback_data"]: item["text"] for row in slack_bot.home_button_rows() for item in row}
        self.assertIn("topic:start", callbacks)
        self.assertEqual(callbacks["topic:start"], "주제 선정 시작")

        state = {"chats": {"C1": {}}}
        started = []
        old_start_background_task = slack_bot.start_background_task
        old_save_state = slack_bot.save_state
        try:
            slack_bot.start_background_task = lambda *args: started.append(args)
            slack_bot.save_state = lambda state: None
            callback = {"message": {"chat": {"id": "C1"}}, "data": "topic:start"}
            slack_bot.handle_callback(state, callback)
        finally:
            slack_bot.start_background_task = old_start_background_task
            slack_bot.save_state = old_save_state

        self.assertEqual(len(started), 1)
        # start_background_task(state, chat_id, job, label, fn) -- the label
        # is what the "진행 중" status message shows while it runs.
        self.assertEqual(started[0][3], "주제 후보 조사")

    def test_card_has_one_select_button_per_candidate(self):
        saved, screens, _, _ = self._patch(candidates=CANDIDATES)
        try:
            slack_bot.send_topic_candidates("C1")
        finally:
            self._restore(saved)

        text, rows = screens[-1]
        select_callbacks = [c for c in self._callbacks(rows) if c.startswith("topic:select:")]
        self.assertEqual(len(select_callbacks), len(CANDIDATES))
        self.assertEqual(select_callbacks[0], f"topic:select:{CANDIDATES[0]['topic_id']}")
        self.assertIn(CANDIDATES[0]["title_hint"], text)
        self.assertIn("점수 88", text)

    def test_card_says_selection_only_records(self):
        saved, screens, _, _ = self._patch(candidates=CANDIDATES)
        try:
            slack_bot.send_topic_candidates("C1")
        finally:
            self._restore(saved)
        self.assertIn("제작은 시작되지 않습니다", screens[-1][0])

    def test_empty_queue_offers_refresh_but_no_select_buttons(self):
        saved, screens, _, _ = self._patch(candidates=[])
        try:
            slack_bot.send_topic_candidates("C1")
        finally:
            self._restore(saved)

        text, rows = screens[-1]
        callbacks = self._callbacks(rows)
        self.assertEqual([c for c in callbacks if c.startswith("topic:select:")], [])
        self.assertIn("topic:refresh", callbacks)
        self.assertIn("추천할 후보가 없습니다", text)

    def test_existing_selection_is_shown(self):
        saved, screens, _, _ = self._patch(
            candidates=CANDIDATES, selection={"title_hint": "이미 고른 주제"},
        )
        try:
            slack_bot.send_topic_candidates("C1")
        finally:
            self._restore(saved)
        self.assertIn("현재 선택: 이미 고른 주제", screens[-1][0])

    def test_select_records_pick_and_confirms(self):
        saved, _, messages, selected_ids = self._patch(
            candidates=CANDIDATES, select_result=CANDIDATES[0],
        )
        try:
            slack_bot.handle_topic_select("C1", CANDIDATES[0]["topic_id"])
        finally:
            self._restore(saved)

        self.assertEqual(selected_ids, [CANDIDATES[0]["topic_id"]])
        self.assertIn(CANDIDATES[0]["title_hint"], messages[-1])
        self.assertIn("제작 시작은 아직 수동입니다", messages[-1])

    def test_stale_topic_id_re_lists_instead_of_raising(self):
        saved, screens, messages, _ = self._patch(candidates=CANDIDATES, select_result=None)
        try:
            slack_bot.handle_topic_select("C1", "사라진-후보")
        finally:
            self._restore(saved)

        self.assertEqual(messages, [])
        self.assertIn("이미 사라진 후보입니다", screens[-1][0])

    def test_select_is_allowed_while_a_job_is_running(self):
        # Picking tomorrow's topic must not wait on today's render.
        state = {"chats": {"C1": {"busy": True, "job_id": "J1", "stage": "render"}}}
        saved, _, messages, selected_ids = self._patch(
            candidates=CANDIDATES, select_result=CANDIDATES[0],
        )
        try:
            slack_bot.handle_callback(state, {
                "message": {"chat": {"id": "C1"}},
                "data": f"topic:select:{CANDIDATES[0]['topic_id']}",
            })
        finally:
            self._restore(saved)

        self.assertEqual(selected_ids, [CANDIDATES[0]["topic_id"]])
        self.assertFalse(any("진행 중입니다" in m for m in messages))


if __name__ == "__main__":
    unittest.main()
