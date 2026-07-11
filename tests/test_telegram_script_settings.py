import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dev" / "src"
sys.path.insert(0, str(SRC))
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

import telegram_bot


class TelegramScriptSettingsTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
