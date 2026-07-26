import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dev" / "src" / "common"
sys.path.insert(0, str(SRC))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

import telegram_bot


class TelegramScriptSettingsTests(unittest.TestCase):
    def capture_actions(self):
        actions = []
        old_send_action_message = telegram_bot.send_action_message
        telegram_bot.send_action_message = lambda chat_id, text, rows: actions.append((chat_id, text, rows))
        self.addCleanup(setattr, telegram_bot, "send_action_message", old_send_action_message)
        return actions

    def test_set_pace_and_duration_are_forwarded_to_all_stages(self):
        job = {}
        messages = []
        old_send_message = telegram_bot.send_message
        try:
            telegram_bot.send_message = lambda chat_id, text: messages.append(text)
            telegram_bot.handle_set(1, job, "/set pace=fast duration=75")
        finally:
            telegram_bot.send_message = old_send_message

        self.assertEqual(job["speech_pace"], "fast")
        self.assertEqual(job["target_duration_sec"], "75")
        env = telegram_bot._build_extra_env(job)
        self.assertEqual(env["SPEECH_PACE"], "fast")
        self.assertEqual(env["ATEMPO"], "1.2")
        self.assertEqual(env["TARGET_DURATION_SEC"], "75")
        self.assertIn("run_auto", messages[0])

    def test_config_summary_hides_chars_per_sec(self):
        summary = telegram_bot.config_summary({"speech_pace": "normal", "target_duration_sec": "60"})
        self.assertIn("SPEECH_PACE=normal", summary)
        self.assertNotIn("CHARS_PER_SEC", summary)

    def test_feedback_strictness_is_forwarded_to_script_stage(self):
        job = {}
        messages = []
        old_send_message = telegram_bot.send_message
        try:
            telegram_bot.send_message = lambda chat_id, text: messages.append(text)
            telegram_bot.handle_set(1, job, "/set feedback_policy=strict feedback_sync=off")
        finally:
            telegram_bot.send_message = old_send_message

        env = telegram_bot._build_extra_env(job)
        self.assertEqual(env["YOUTUBE_FEEDBACK_STRICTNESS"], "strict")
        self.assertEqual(env["YOUTUBE_FEEDBACK_AUTO_SYNC"], "false")

    def test_invalid_goal_does_not_replace_existing_job(self):
        job = {"job_id": "existing", "stage": "await_script_approval", "topic": "기존 주제"}
        messages = []
        old_send_message = telegram_bot.send_message
        old_run_command = telegram_bot.run_command
        try:
            telegram_bot.send_message = lambda chat_id, text: messages.append(text)
            telegram_bot.run_command = lambda *args, **kwargs: self.fail("invalid goal must not run")
            telegram_bot.handle_run_goal(1, job, "/run_goal invalid_goal 수면")
        finally:
            telegram_bot.send_message = old_send_message
            telegram_bot.run_command = old_run_command
        self.assertEqual(job, {"job_id": "existing", "stage": "await_script_approval", "topic": "기존 주제"})
        self.assertIn("목표 입력 오류", messages[-1])

    def test_set_without_values_opens_category_menu(self):
        actions = self.capture_actions()

        telegram_bot.handle_set(1, {}, "/set")

        self.assertEqual(len(actions), 1)
        callbacks = [button["callback_data"] for row in actions[0][2] for button in row]
        self.assertIn("cfg:cat:caption", callbacks)
        self.assertIn("cfg:cat:frame", callbacks)
        self.assertIn("cfg:all", callbacks)
        self.assertNotIn("현재 주요 컨피그", actions[0][1])

    def test_numeric_setting_accepts_only_the_next_plain_message(self):
        actions = self.capture_actions()
        job = {}

        telegram_bot.begin_config_edit(1, job, "font_size")
        handled = telegram_bot.apply_config_edit_message(1, job, {"text": "84"})

        self.assertTrue(handled)
        self.assertEqual(job["caption_font_size"], "84")
        self.assertNotIn("config_edit_key", job)
        self.assertIn("현재값: 84", actions[-1][1])

    def test_invalid_numeric_setting_stays_in_edit_mode(self):
        messages = []
        old_send_message = telegram_bot.send_message
        telegram_bot.send_message = lambda chat_id, text: messages.append(text)
        self.addCleanup(setattr, telegram_bot, "send_message", old_send_message)
        job = {"config_edit_key": "duration"}

        handled = telegram_bot.apply_config_edit_message(1, job, {"text": "zero"})

        self.assertTrue(handled)
        self.assertEqual(job["config_edit_key"], "duration")
        self.assertIn("설정 오류", messages[0])

    def test_choice_callback_saves_value_and_default_button_removes_override(self):
        actions = self.capture_actions()
        old_api = telegram_bot.api
        telegram_bot.api = lambda *args, **kwargs: {"ok": True}
        self.addCleanup(setattr, telegram_bot, "api", old_api)
        state = {"chats": {"1": {}}}
        callback = {"id": "cb", "message": {"chat": {"id": 1}}, "data": "cfg:pick:pace:2"}

        telegram_bot.handle_callback(state, callback)
        self.assertEqual(state["chats"]["1"]["speech_pace"], "fast")
        self.assertIn("현재값: fast", actions[-1][1])

        callback["data"] = "cfg:default:pace"
        telegram_bot.handle_callback(state, callback)
        self.assertNotIn("speech_pace", state["chats"]["1"])
        self.assertIn("기본값", actions[-1][1])

    def test_set_all_command_prints_full_config_without_opening_menu(self):
        messages = []
        actions = self.capture_actions()
        old_send_message = telegram_bot.send_message
        telegram_bot.send_message = lambda chat_id, text: messages.append(text)
        self.addCleanup(setattr, telegram_bot, "send_message", old_send_message)

        telegram_bot.handle_message({"chats": {}}, {"chat": {"id": 1}, "text": "/set_all"})

        self.assertEqual(actions, [])
        self.assertIn("[자막]", messages[0])
        self.assertIn("CAPTION_OFFSET_Y", messages[0])


if __name__ == "__main__":
    unittest.main()
