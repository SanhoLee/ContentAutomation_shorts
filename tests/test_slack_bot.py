import contextlib
import importlib.util
import io
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dev" / "src" / "common"
sys.path.insert(0, str(SRC))
# dev/config.sh puts common/, youtube/ and instagram/ all on PYTHONPATH so
# the pipeline modules can import each other; tests must do the same.
sys.path.insert(0, str(ROOT / "dev" / "src" / "youtube"))
import slack_bot

PROD_SPEC = importlib.util.spec_from_file_location("prod_slack_bot", ROOT / "prod" / "src" / "slack_bot.py")
prod_slack_bot = importlib.util.module_from_spec(PROD_SPEC)
PROD_SPEC.loader.exec_module(prod_slack_bot)


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
        for module in (slack_bot, prod_slack_bot):
            notices = []
            original_state = module._STATE
            old_allow = module._allow
            old_send_message = module.send_message
            old_handle_callback = module.handle_callback
            old_save_state = module.save_state
            try:
                module._STATE = {"chats": {}}
                module._allow = lambda channel_id, user_id: True
                module.send_message = lambda channel_id, text: notices.append(text)
                module.handle_callback = lambda state, callback: True
                module.save_state = lambda state: None
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    module._dispatch_action({
                        "channel": {"id": "C1"},
                        "user": {"id": "U1"},
                        "message": {"ts": "123.456"},
                        "actions": [{"value": "show_status"}],
                    })
            finally:
                module._STATE = original_state
                module._allow = old_allow
                module.send_message = old_send_message
                module.handle_callback = old_handle_callback
                module.save_state = old_save_state
            self.assertIn("요청됨: 현재 작업 상태 확인", notices)
            self.assertIn("slack_action_requested", output.getvalue())
            self.assertIn("slack_action_finished", output.getvalue())
            self.assertIn('result="handled"', output.getvalue())

    def test_button_error_screen_has_safe_recovery_navigation(self):
        for module in (slack_bot, prod_slack_bot):
            screens = []
            old_send_action = module.send_action_message
            try:
                module.send_action_message = lambda channel_id, text, rows: screens.append((text, rows))
                module._send_recovery_error("C1", "open_settings", RuntimeError("설정 오류"))
            finally:
                module.send_action_message = old_send_action
            text, rows = screens[-1]
            self.assertIn("요청 처리 실패", text)
            callbacks = {item["callback_data"] for row in rows for item in row}
            self.assertEqual(callbacks, {"show_home", "open_settings", "show_status"})

    def test_home_screen_exposes_safe_content_entry_points(self):
        expected = {
            "start_content:review", "start_content:auto", "start_content:trend",
            "show_status", "open_settings", "show_home",
        }
        for module in (slack_bot, prod_slack_bot):
            callbacks = {
                item["callback_data"]
                for row in module.home_button_rows()
                for item in row
            }
            self.assertTrue(expected.issubset(callbacks))
            text = module.home_screen_text({})
            self.assertIn("Brain50 콘텐츠 제작 홈", text)
            self.assertIn("주제를 입력하고 실행 전 확인", text)

    def test_start_button_waits_for_topic_and_confirmation(self):
        for module in (slack_bot, prod_slack_bot):
            state = {"chats": {"C1": {}}}
            screens, started = [], []
            old_send_action = module.send_action_message
            old_start_background = module.start_background_task
            old_save_state = module.save_state
            try:
                module.send_action_message = lambda channel_id, text, rows: screens.append((text, rows))
                module.start_background_task = lambda *args: started.append(args)
                module.save_state = lambda state: None

                callback = {"message": {"chat": {"id": "C1"}}, "data": "start_content:review"}
                module.handle_callback(state, callback)
                job = state["chats"]["C1"]
                self.assertEqual(job["start_draft"], {"mode": "review"})
                self.assertFalse(started)
                self.assertIn("1/2 주제 입력", screens[-1][0])

                module.handle_message(state, {"chat": {"id": "C1"}, "text": "오메가3와 기억력"})
                self.assertEqual(job["start_draft"]["topic"], "오메가3와 기억력")
                self.assertFalse(started)
                self.assertIn("2/2 실행 확인", screens[-1][0])
                confirm_callbacks = {item["callback_data"] for row in screens[-1][1] for item in row}
                self.assertIn("start_confirm:review", confirm_callbacks)
                self.assertIn("start_reenter_topic", confirm_callbacks)
                self.assertIn("start_cancel", confirm_callbacks)

                callback["data"] = "start_confirm:review"
                module.handle_callback(state, callback)
                self.assertTrue(started)
            finally:
                module.send_action_message = old_send_action
                module.start_background_task = old_start_background
                module.save_state = old_save_state

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
        for module in (slack_bot, prod_slack_bot):
            for text, mode in commands.items():
                state = {"chats": {"C1": {}}}
                screens, started = [], []
                old_send_action = module.send_action_message
                old_start_background = module.start_background_task
                try:
                    module.send_action_message = lambda channel_id, body, rows: screens.append(body)
                    module.start_background_task = lambda *args: started.append(args)
                    module.handle_message(state, {"chat": {"id": "C1"}, "text": text})
                finally:
                    module.send_action_message = old_send_action
                    module.start_background_task = old_start_background
                draft = state["chats"]["C1"]["start_draft"]
                self.assertEqual(draft["mode"], mode)
                self.assertTrue(draft["topic"].startswith("주제"))
                self.assertFalse(started)
                self.assertIn("2/2 실행 확인", screens[-1])

    def test_start_flow_back_preserves_existing_job(self):
        for module in (slack_bot, prod_slack_bot):
            job = {"job_id": "existing", "topic": "기존 주제", "stage": "await_caption_approval"}
            state = {"chats": {"C1": job}}
            homes = []
            old_send_action = module.send_action_message
            old_send_home = module.send_home_screen
            old_save_state = module.save_state
            try:
                module.send_action_message = lambda *args, **kwargs: None
                module.send_home_screen = lambda *args, **kwargs: homes.append(args)
                module.save_state = lambda state: None
                module.handle_callback(state, {"message": {"chat": {"id": "C1"}}, "data": "start_content:auto"})
                module.handle_callback(state, {"message": {"chat": {"id": "C1"}}, "data": "start_cancel"})
            finally:
                module.send_action_message = old_send_action
                module.send_home_screen = old_send_home
                module.save_state = old_save_state
            self.assertNotIn("start_draft", job)
            self.assertEqual(job["job_id"], "existing")
            self.assertEqual(job["stage"], "await_caption_approval")
            self.assertTrue(homes)

    def test_confirmed_start_uses_the_selected_mode(self):
        for module in (slack_bot, prod_slack_bot):
            calls = []
            old_start_background = module.start_background_task
            old_handle_run = module.handle_run
            old_handle_run_auto = module.handle_run_auto
            try:
                module.start_background_task = lambda state, chat_id, job, label, target: target()
                module.handle_run = lambda chat_id, job, text, trend=False: calls.append(("trend" if trend else "review", text))
                module.handle_run_auto = lambda chat_id, job, text: calls.append(("auto", text))
                for mode in ("review", "auto", "trend"):
                    job = {"start_draft": {"mode": mode, "topic": "테스트 주제"}}
                    module.confirm_start_flow({"chats": {"C1": job}}, "C1", job, mode)
            finally:
                module.start_background_task = old_start_background
                module.handle_run = old_handle_run
                module.handle_run_auto = old_handle_run_auto
            self.assertEqual(calls, [
                ("review", "/run 테스트 주제"),
                ("auto", "/run_auto 테스트 주제"),
                ("trend", "/trend 테스트 주제"),
            ])

    def test_startup_posts_a_top_level_home_when_channel_is_configured(self):
        for module in (slack_bot, prod_slack_bot):
            old_channel, old_state = module.ALLOWED_CHANNEL_ID, module._STATE
            old_send_home, old_save_state = module.send_home_screen, module.save_state
            calls = []
            try:
                module.ALLOWED_CHANNEL_ID = "C1"
                module._STATE = {"chats": {"C1": {"slack_thread_ts": "old-thread"}}}
                module.send_home_screen = lambda *args, **kwargs: calls.append((args, kwargs))
                module.save_state = lambda state: None
                self.assertTrue(module.announce_startup_home())
            finally:
                module.ALLOWED_CHANNEL_ID = old_channel
                module._STATE = old_state
                module.send_home_screen = old_send_home
                module.save_state = old_save_state
            self.assertTrue(calls)
            self.assertTrue(calls[0][1]["top_level"])

    def test_app_home_publishes_the_same_entry_points(self):
        class FakeClient:
            def __init__(self):
                self.calls = []

            def views_publish(self, **kwargs):
                self.calls.append(kwargs)

        for module in (slack_bot, prod_slack_bot):
            old_channel, old_state = module.ALLOWED_CHANNEL_ID, module._STATE
            client = FakeClient()
            try:
                module.ALLOWED_CHANNEL_ID = "C1"
                module._STATE = {"chats": {"C1": {}}}
                module.publish_home("U1", client=client)
            finally:
                module.ALLOWED_CHANNEL_ID = old_channel
                module._STATE = old_state
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
        for module in (slack_bot, prod_slack_bot):
            blocks = module._blocks(module.home_screen_text({}), module.home_button_rows())
            action_ids = [
                element["action_id"]
                for block in blocks if block.get("type") == "actions"
                for element in block["elements"]
            ]
            self.assertEqual(len(action_ids), len(set(action_ids)))
            self.assertTrue(all(module.re.fullmatch(r"workflow_action_\d+_\d+", action_id) for action_id in action_ids))

    def test_event_conversion_preserves_text_and_file(self):
        event = {"channel": "C1", "text": "/run 테스트", "files": [{"url_private": "https://example.invalid/a"}]}
        message = slack_bot._event_to_message(event)
        self.assertEqual(message["chat"]["id"], "C1")
        self.assertEqual(message["text"], "/run 테스트")
        self.assertIn("document", message)

    def test_slack_source_does_not_import_or_reference_telegram_bot(self):
        for relative_path in ("dev/src/common/slack_bot.py", "prod/src/slack_bot.py"):
            source = (ROOT / relative_path).read_text(encoding="utf-8")
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

    def test_prod_registers_documented_set_commands(self):
        command_names = {name for name, _ in prod_slack_bot.command_specs()}
        self.assertIn("set", command_names)
        self.assertIn("set_all", command_names)

        job = {}
        messages = []
        old_send_message = prod_slack_bot.send_message
        try:
            prod_slack_bot.send_message = lambda channel_id, text: messages.append(text)
            prod_slack_bot.handle_set("C1", job, "/set font_size=64 web=off")
        finally:
            prod_slack_bot.send_message = old_send_message
        self.assertEqual(job["caption_font_size"], "64")
        self.assertFalse(job["web_research"])

    def test_slack_uses_non_reserved_app_status_command(self):
        for module in (slack_bot, prod_slack_bot):
            command_names = {name for name, _ in module.command_specs()}
            self.assertIn("app_status", command_names)
            self.assertNotIn("status", command_names)
            self.assertIn("/app_status", module.help_text())

            state = {"chats": {"C1": {"busy": "렌더링", "stage": "await_render_approval"}}}
            messages = []
            old_send_message, old_send_action_message = module.send_message, module.send_action_message
            try:
                module.send_message = lambda channel_id, text: messages.append(text)
                module.send_action_message = lambda channel_id, text, rows: messages.append(text)
                module.handle_message(state, {"chat": {"id": "C1"}, "text": "/app_status"})
            finally:
                module.send_message = old_send_message
                module.send_action_message = old_send_action_message
            self.assertTrue(any("최종 영상 확인" in message for message in messages))

    def test_every_review_stage_has_safe_navigation(self):
        for module in (slack_bot, prod_slack_bot):
            for index, stage in enumerate(module.WORKFLOW_STAGES):
                callbacks = {
                    item["callback_data"]
                    for row in module.approval_buttons(stage)
                    for item in row
                }
                self.assertIn(f"approve:{stage}", callbacks)
                self.assertIn(f"auto_finish:{stage}", callbacks)
                self.assertIn("show_status", callbacks)
                self.assertIn("cancel_all", callbacks)
                if index:
                    self.assertIn(f"back:{stage}:{module.WORKFLOW_STAGES[index - 1]}", callbacks)

    def test_progress_card_summarizes_current_stage(self):
        for module in (slack_bot, prod_slack_bot):
            text = module.workflow_status_text({
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
            "await_render_config": "await_render_approval",
            "await_render_approval": "await_upload_meta_approval",
            "await_upload_meta_approval": "done",
        }
        sender_names = ("send_tts", "send_caption", "send_broll", "send_render_ready", "send_rendered_video", "send_upload_meta", "send_message")
        for module in (slack_bot, prod_slack_bot):
            originals = {name: getattr(module, name) for name in sender_names}
            old_run_command, old_run_render = module.run_command, module.run_render
            try:
                module.run_command = lambda *a, **k: None
                for name in sender_names:
                    setattr(module, name, lambda *a, **k: None)
                module.run_render = lambda chat_id, job: job.update(stage="await_render_approval")
                for stage, next_stage in expected_next.items():
                    job = {"job_id": f"job-{stage}", "topic": "테스트", "stage": stage}
                    module.run_next_stage("C1", job)
                    self.assertEqual(job["stage"], next_stage)
            finally:
                module.run_command = old_run_command
                module.run_render = old_run_render
                for name, original in originals.items():
                    setattr(module, name, original)

    def test_auto_finish_can_resume_from_every_review_stage(self):
        # dev's slack_bot drives this through pipeline_flow.STAGES, which now
        # includes x_thread (right after script) and x_post (right after
        # upload). prod/src/slack_bot.py has its own hardcoded
        # run_remaining_to_upload, wholly independent of pipeline_flow, and
        # was not touched -- it still expects the pre-X-posting sequence.
        dev_expected_commands = {
            "await_script_approval": ["x_thread.sh", "1_tts.sh", "1_caption.sh", "1_broll.sh", "3_upload.sh", "x_post.sh"],
            "await_tts_approval": ["1_caption.sh", "1_broll.sh", "3_upload.sh", "x_post.sh"],
            "await_caption_approval": ["1_broll.sh", "3_upload.sh", "x_post.sh"],
            "await_broll_approval": ["3_upload.sh", "x_post.sh"],
            "await_render_config": ["3_upload.sh", "x_post.sh"],
            "await_render_approval": ["3_upload.sh", "x_post.sh"],
            "await_upload_meta_approval": ["3_upload.sh", "x_post.sh"],
        }
        prod_expected_commands = {
            "await_script_approval": ["1_tts.sh", "1_caption.sh", "1_broll.sh", "3_upload.sh"],
            "await_tts_approval": ["1_caption.sh", "1_broll.sh", "3_upload.sh"],
            "await_caption_approval": ["1_broll.sh", "3_upload.sh"],
            "await_broll_approval": ["3_upload.sh"],
            "await_render_config": ["3_upload.sh"],
            "await_render_approval": ["3_upload.sh"],
            "await_upload_meta_approval": ["3_upload.sh"],
        }
        render_stages = {
            "await_script_approval", "await_tts_approval", "await_caption_approval",
            "await_broll_approval", "await_render_config",
        }
        # This test is about which stages run, not about the stage checks --
        # those have their own coverage in test_stage_guard.py, and the fake
        # job directories here hold no artifacts for them to inspect.
        real_check = slack_bot.pipeline_flow.stage_guard.check
        slack_bot.pipeline_flow.stage_guard.check = lambda *a, **k: (True, "")
        try:
            self._assert_auto_finish_resumes(slack_bot, dev_expected_commands, render_stages)
            self._assert_auto_finish_resumes(prod_slack_bot, prod_expected_commands, render_stages)
        finally:
            slack_bot.pipeline_flow.stage_guard.check = real_check

    def _assert_auto_finish_resumes(self, module, expected_commands, render_stages):
        for stage in module.WORKFLOW_STAGES:
            commands, renders = [], []
            old_run_command = module.run_command
            old_run_render_silent = module._run_render_silent
            old_send_message = module.send_message
            try:
                module.run_command = lambda args, *a, **k: commands.append(Path(args[0]).name)
                module._run_render_silent = lambda *a, **k: renders.append("render")
                module.send_message = lambda *a, **k: None
                job = {"job_id": f"job-{stage}", "topic": "테스트", "stage": stage}
                module.run_remaining_to_upload("C1", job)
            finally:
                module.run_command = old_run_command
                module._run_render_silent = old_run_render_silent
                module.send_message = old_send_message
            self.assertEqual(commands, expected_commands[stage])
            self.assertEqual(len(renders), 1 if stage in render_stages else 0)
            self.assertEqual(job["stage"], "done")

    def test_cancel_request_is_available_while_busy(self):
        for module in (slack_bot, prod_slack_bot):
            job_id = f"cancel-{module.__name__}"
            job = {"job_id": job_id, "stage": "running_after_review", "busy": "끝까지 자동 처리"}
            messages = []
            old_send_message = module.send_message
            try:
                module.send_message = lambda channel_id, text: messages.append(text)
                module.request_workflow_cancel("C1", job)
                self.assertTrue(job["cancel_requested"])
                with self.assertRaises(module.WorkflowCancelled):
                    module.run_command(["unused"], job_id)
            finally:
                module.CANCELLED_JOB_IDS.discard(job_id)
                module.send_message = old_send_message
            self.assertTrue(any("취소 요청" in message for message in messages))

    def test_destructive_buttons_require_confirmation(self):
        for module in (slack_bot, prod_slack_bot):
            state = {"chats": {"C1": {"job_id": "job-1", "topic": "테스트", "stage": "await_caption_approval"}}}
            confirmations, started = [], []
            old_send_action_message = module.send_action_message
            old_start_background_task = module.start_background_task
            old_save_state = module.save_state
            try:
                module.send_action_message = lambda channel_id, text, rows: confirmations.append(rows)
                module.start_background_task = lambda *args: started.append(args)
                module.save_state = lambda state: None
                callback = {"message": {"chat": {"id": "C1"}}, "data": "auto_finish:await_caption_approval"}
                module.handle_callback(state, callback)
                self.assertFalse(started)
                confirm_callbacks = {item["callback_data"] for row in confirmations[-1] for item in row}
                self.assertIn("auto_finish_confirm:await_caption_approval", confirm_callbacks)

                callback["data"] = "auto_finish_confirm:await_caption_approval"
                module.handle_callback(state, callback)
                self.assertTrue(started)

                confirmations.clear()
                callback["data"] = "cancel_all"
                module.handle_callback(state, callback)
                cancel_callbacks = {item["callback_data"] for row in confirmations[-1] for item in row}
                self.assertIn("cancel_confirm", cancel_callbacks)
            finally:
                module.send_action_message = old_send_action_message
                module.start_background_task = old_start_background_task
                module.save_state = old_save_state


class MaybePostXThreadTests(unittest.TestCase):
    """Auto-posting after upload must never mask the pipeline's own result.

    post_thread() signals by raising, and owns resume/double-post safety
    itself, so this hook only has to translate outcomes into messages.
    """

    def _patch(self, post_result=None, post_error=None):
        calls = []
        old_post = slack_bot.x_poster.post_thread
        old_send_message = slack_bot.send_message

        def fake_post(job_dir):
            calls.append(job_dir)
            if post_error is not None:
                raise post_error
            return post_result

        messages = []
        slack_bot.x_poster.post_thread = fake_post
        slack_bot.send_message = lambda channel_id, text: messages.append(text)
        return calls, messages, old_post, old_send_message

    def _restore(self, old_post, old_send_message):
        slack_bot.x_poster.post_thread = old_post
        slack_bot.send_message = old_send_message

    def test_skips_when_job_not_done(self):
        calls, messages, old_post, old_send = self._patch()
        try:
            slack_bot._maybe_post_x_thread("C1", {"job_id": "J1", "stage": "await_render_approval"})
        finally:
            self._restore(old_post, old_send)
        self.assertEqual(calls, [])
        self.assertEqual(messages, [])

    def test_skips_when_no_job_id(self):
        calls, messages, old_post, old_send = self._patch()
        try:
            slack_bot._maybe_post_x_thread("C1", {"stage": "done"})
        finally:
            self._restore(old_post, old_send)
        self.assertEqual(calls, [])
        self.assertEqual(messages, [])

    def test_success_reports_thread_url_and_tweet_count(self):
        result = {"tweet_ids": ["1", "2", "3"], "thread_url": "https://x.com/i/web/status/1"}
        calls, messages, old_post, old_send = self._patch(post_result=result)
        try:
            slack_bot._maybe_post_x_thread("C1", {"job_id": "J1", "stage": "done"})
        finally:
            self._restore(old_post, old_send)
        self.assertEqual(len(calls), 1)
        self.assertIn("게시 완료", messages[-1])
        self.assertIn("https://x.com/i/web/status/1", messages[-1])
        self.assertIn("3개", messages[-1])

    def test_failure_is_reported_not_raised_and_points_at_x_post(self):
        calls, messages, old_post, old_send = self._patch(
            post_error=RuntimeError("2/4개 게시 후 실패: 429"),
        )
        try:
            slack_bot._maybe_post_x_thread("C1", {"job_id": "J1", "stage": "done"})
        finally:
            self._restore(old_post, old_send)
        self.assertIn("자동 게시 실패", messages[-1])
        self.assertIn("429", messages[-1])
        self.assertIn("/x_post", messages[-1])

    def test_unexpected_exception_does_not_escape(self):
        calls, messages, old_post, old_send = self._patch(post_error=ValueError("boom"))
        try:
            slack_bot._maybe_post_x_thread("C1", {"job_id": "J1", "stage": "done"})
        finally:
            self._restore(old_post, old_send)
        self.assertIn("boom", messages[-1])


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
