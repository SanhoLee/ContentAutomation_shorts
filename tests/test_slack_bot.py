import importlib.util
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dev" / "src"
sys.path.insert(0, str(SRC))
import slack_bot

PROD_SPEC = importlib.util.spec_from_file_location("prod_slack_bot", ROOT / "prod" / "src" / "slack_bot.py")
prod_slack_bot = importlib.util.module_from_spec(PROD_SPEC)
PROD_SPEC.loader.exec_module(prod_slack_bot)


class SlackBotTests(unittest.TestCase):
    def test_block_buttons_keep_workflow_callback_data(self):
        blocks = slack_bot._blocks("승인", [[{"text": "승인", "callback_data": "approve:await_script_approval"}]])
        button = blocks[1]["elements"][0]
        self.assertEqual(button["action_id"], "workflow_action")
        self.assertEqual(button["value"], "approve:await_script_approval")

    def test_event_conversion_preserves_text_and_file(self):
        event = {"channel": "C1", "text": "/run 테스트", "files": [{"url_private": "https://example.invalid/a"}]}
        message = slack_bot._event_to_message(event)
        self.assertEqual(message["chat"]["id"], "C1")
        self.assertEqual(message["text"], "/run 테스트")
        self.assertIn("document", message)

    def test_slack_source_does_not_import_or_reference_telegram_bot(self):
        for relative_path in ("dev/src/slack_bot.py", "prod/src/slack_bot.py"):
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


if __name__ == "__main__":
    unittest.main()
