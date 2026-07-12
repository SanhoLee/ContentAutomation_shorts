import os
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "dev" / "src"
sys.path.insert(0, str(SRC))

import script_runtime


class ScriptRuntimeSettingsTests(unittest.TestCase):
    KEYS = (
        "SPEECH_PACE", "ATEMPO", "CHARS_PER_SEC", "TARGET_DURATION_SEC",
        "CLAUDE_MODEL", "CLAUDE_SCRIPT_MODEL", "CLAUDE_STRATEGY_MODEL",
        "CLAUDE_STRATEGY_FALLBACK_MODELS", "CLAUDE_STRATEGY_MAX_TOKENS",
    )

    def load_settings(self, **values):
        previous = {key: os.environ.get(key) for key in self.KEYS}
        try:
            for key in self.KEYS:
                os.environ.pop(key, None)
            os.environ.update({key: str(value) for key, value in values.items()})
            return script_runtime.load_runtime_settings()
        finally:
            for key in self.KEYS:
                os.environ.pop(key, None)
                if previous[key] is not None:
                    os.environ[key] = previous[key]

    def test_stage1_defaults_use_resilient_model_order_and_budget(self):
        settings = self.load_settings()
        self.assertEqual(settings.claude_strategy_model, "claude-haiku-4-5-20251001")
        self.assertEqual(
            settings.claude_strategy_fallback_models,
            ("claude-sonnet-4-5-20250929",),
        )
        self.assertEqual(settings.claude_script_model, "claude-sonnet-4-6")
        self.assertEqual(settings.claude_strategy_max_tokens, 2000)

    def test_pace_controls_length_without_chars_per_sec(self):
        settings = self.load_settings(SPEECH_PACE="normal", TARGET_DURATION_SEC=60)
        self.assertEqual(settings.speech_pace, "normal")
        self.assertEqual(settings.total_chars, int(60 * settings.script_density))

    def test_fast_pace_allows_more_text_than_slow(self):
        slow = self.load_settings(SPEECH_PACE="slow", TARGET_DURATION_SEC=60)
        fast = self.load_settings(SPEECH_PACE="fast", TARGET_DURATION_SEC=60)
        self.assertGreater(fast.total_chars, slow.total_chars)
        self.assertGreater(fast.atempo, slow.atempo)

    def test_explicit_pace_ignores_legacy_char_settings(self):
        baseline = self.load_settings(SPEECH_PACE="fast", TARGET_DURATION_SEC=60)
        legacy_present = self.load_settings(
            SPEECH_PACE="fast", TARGET_DURATION_SEC=60, ATEMPO=0.5, CHARS_PER_SEC=20
        )
        self.assertEqual(legacy_present.total_chars, baseline.total_chars)
        self.assertEqual(legacy_present.atempo, baseline.atempo)

    def test_legacy_settings_preserve_previous_formula(self):
        settings = self.load_settings(TARGET_DURATION_SEC=60, ATEMPO=1.1, CHARS_PER_SEC=5)
        self.assertEqual(settings.speech_pace, "legacy")
        self.assertEqual(settings.total_chars, int(60 * 1.1 * 5))


if __name__ == "__main__":
    unittest.main()
