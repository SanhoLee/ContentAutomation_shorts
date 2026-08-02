import contextlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))
sys.path.insert(0, str(ROOT / "dev" / "src" / "common" / "adapters"))
sys.path.insert(0, str(ROOT / "dev" / "src" / "instagram"))

import x_photo_card as xpc


@contextlib.contextmanager
def fake_card_render(render):
    """Stand in for the real card_render module.

    build_thread_photo imports card_render lazily (Pillow lives behind it),
    so the seam to patch is sys.modules, not an attribute on x_photo_card.
    """
    module = types.ModuleType("card_render")
    module.render_card = render
    saved = sys.modules.get("card_render")
    sys.modules["card_render"] = module
    try:
        yield
    finally:
        if saved is None:
            del sys.modules["card_render"]
        else:
            sys.modules["card_render"] = saved


@contextlib.contextmanager
def no_card_render():
    """Simulate a host with no Pillow installed (import raises ImportError)."""
    saved = sys.modules.get("card_render")
    sys.modules["card_render"] = None
    try:
        yield
    finally:
        if saved is None:
            del sys.modules["card_render"]
        else:
            sys.modules["card_render"] = saved


class BuildThreadPhotoTests(unittest.TestCase):
    def test_uses_hook_text_and_calls_render_card(self):
        calls = []

        def render(text, output_path, canvas_size=None, font_path=None):
            calls.append((text, output_path, canvas_size))
            Path(output_path).write_bytes(b"fake-png")
            return Path(output_path)

        with fake_card_render(render), tempfile.TemporaryDirectory() as tmp:
            package = {"hook": "치매, 원인이 여러분이 생각하는 그게 아닙니다."}
            result = xpc.build_thread_photo(package, Path(tmp))

            self.assertIsNotNone(result)
            self.assertEqual(len(calls), 1)
            text, output_path, canvas_size = calls[0]
            self.assertEqual(text, package["hook"])
            self.assertEqual(canvas_size, xpc.THREAD_PHOTO_SIZE)
            self.assertEqual(Path(output_path).name, xpc.PHOTO_FILENAME)

    def test_falls_back_to_video_meta_title_when_no_hook(self):
        calls = []

        def render(text, output_path, canvas_size=None, font_path=None):
            calls.append(text)
            Path(output_path).write_bytes(b"fake-png")
            return Path(output_path)

        with fake_card_render(render), tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            (job_dir / "video_meta.json").write_text(
                json.dumps({"title": "치매 예방 습관"}, ensure_ascii=False), encoding="utf-8",
            )
            result = xpc.build_thread_photo({}, job_dir)

            self.assertIsNotNone(result)
            self.assertEqual(calls, ["치매 예방 습관"])

    def test_no_hook_and_no_video_meta_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(xpc.build_thread_photo({}, Path(tmp)))

    def test_render_failure_returns_none(self):
        def failing_render(text, output_path, canvas_size=None, font_path=None):
            raise OSError("no font available")

        with fake_card_render(failing_render), tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(xpc.build_thread_photo({"hook": "훅"}, Path(tmp)))

    def test_missing_pillow_returns_none_instead_of_raising(self):
        # The card is decoration; a host without Pillow must still be able to
        # build and post the thread.
        with no_card_render(), tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(xpc.build_thread_photo({"hook": "훅"}, Path(tmp)))


if __name__ == "__main__":
    unittest.main()
