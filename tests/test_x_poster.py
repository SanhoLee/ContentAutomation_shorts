import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))
sys.path.insert(0, str(ROOT / "dev" / "src" / "common" / "adapters"))

import x_poster as xp


def _write_thread(job_dir: Path, tweets, **extra):
    payload = {
        "job_id": "J001",
        "tweets": [{"index": i + 1, "text": t} for i, t in enumerate(tweets)],
        "char_counts": [len(t) for t in tweets],
        "method": "rule_v1",
        **extra,
    }
    (job_dir / "x_thread.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return payload


class PostThreadTests(unittest.TestCase):
    def setUp(self):
        self._token_patch = mock.patch("x_auth.get_valid_access_token", return_value="test-bearer")
        self._token_patch.start()

    def tearDown(self):
        self._token_patch.stop()

    def _mock_responses(self, ids):
        responses = []
        for tweet_id in ids:
            res = mock.Mock()
            res.raise_for_status = mock.Mock()
            res.json.return_value = {"data": {"id": tweet_id}}
            responses.append(res)
        return responses

    def test_missing_thread_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                xp.post_thread(tmp)

    def test_already_posted_thread_refuses_to_repost(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            _write_thread(job_dir, ["hello"], posted=True, posted_at="2026-01-01T00:00:00+00:00")
            with mock.patch("requests.post", side_effect=AssertionError("should not post")):
                with self.assertRaises(RuntimeError):
                    xp.post_thread(job_dir)

    def test_dry_run_makes_no_network_call_and_no_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            _write_thread(job_dir, ["one", "two"])
            before = (job_dir / "x_thread.json").read_text(encoding="utf-8")
            with mock.patch("requests.post", side_effect=AssertionError("should not post")):
                xp.post_thread(job_dir, dry_run=True)
            after = (job_dir / "x_thread.json").read_text(encoding="utf-8")
            self.assertEqual(before, after)

    def test_posts_sequentially_as_reply_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            _write_thread(job_dir, ["first", "second", "third"])
            responses = self._mock_responses(["id1", "id2", "id3"])
            with mock.patch("requests.post", side_effect=responses) as post:
                payload = xp.post_thread(job_dir)

            self.assertEqual(payload["tweet_ids"], ["id1", "id2", "id3"])
            self.assertTrue(payload["posted"])
            self.assertEqual(payload["thread_url"], "https://x.com/i/web/status/id1")

            calls = post.call_args_list
            self.assertNotIn("reply", calls[0].kwargs["json"])
            self.assertEqual(calls[1].kwargs["json"]["reply"]["in_reply_to_tweet_id"], "id1")
            self.assertEqual(calls[2].kwargs["json"]["reply"]["in_reply_to_tweet_id"], "id2")

            on_disk = json.loads((job_dir / "x_thread.json").read_text(encoding="utf-8"))
            self.assertEqual(on_disk["tweet_ids"], ["id1", "id2", "id3"])

    def test_failure_midway_persists_partial_progress_and_resumes(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            _write_thread(job_dir, ["first", "second", "third"])

            ok_response = self._mock_responses(["id1"])[0]
            with mock.patch("requests.post", side_effect=[ok_response, ConnectionError("network blip")]):
                with self.assertRaises(RuntimeError):
                    xp.post_thread(job_dir)

            on_disk = json.loads((job_dir / "x_thread.json").read_text(encoding="utf-8"))
            self.assertEqual(on_disk["tweet_ids"], ["id1"])
            self.assertFalse(on_disk.get("posted", False))
            self.assertIn("network blip", on_disk["post_error"])

            # Resuming must not repost "first" -- only "second" and "third" go out,
            # and "second" replies to the already-posted id1.
            responses = self._mock_responses(["id2", "id3"])
            with mock.patch("requests.post", side_effect=responses) as post:
                payload = xp.post_thread(job_dir)

            self.assertEqual(payload["tweet_ids"], ["id1", "id2", "id3"])
            self.assertTrue(payload["posted"])
            self.assertNotIn("post_error", payload)
            first_call_body = post.call_args_list[0].kwargs["json"]
            self.assertEqual(first_call_body["text"], "second")
            self.assertEqual(first_call_body["reply"]["in_reply_to_tweet_id"], "id1")


if __name__ == "__main__":
    unittest.main()
