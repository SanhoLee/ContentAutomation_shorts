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


class ThreadPhotoTests(unittest.TestCase):
    """The card rides on the lead tweet only, and is never worth the thread."""

    def setUp(self):
        self._token_patch = mock.patch("x_auth.get_valid_access_token", return_value="test-bearer")
        self._token_patch.start()

    def tearDown(self):
        self._token_patch.stop()

    @staticmethod
    def _tweet_responses(ids):
        responses = []
        for tweet_id in ids:
            res = mock.Mock()
            res.raise_for_status = mock.Mock()
            res.json.return_value = {"data": {"id": tweet_id}}
            responses.append(res)
        return responses

    @staticmethod
    def _photo(job_dir):
        path = job_dir / "x_thread_photo.png"
        path.write_bytes(b"fake-png")
        return path

    def test_media_id_is_attached_to_the_first_tweet_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            photo = self._photo(job_dir)
            _write_thread(job_dir, ["first", "second"], photo_path=str(photo))

            responses = self._tweet_responses(["id1", "id2"])
            with mock.patch.object(xp, "_upload_media", return_value="media-9") as upload:
                with mock.patch("requests.post", side_effect=responses) as post:
                    xp.post_thread(job_dir)

            self.assertEqual(upload.call_count, 1)
            bodies = [call.kwargs["json"] for call in post.call_args_list]
            self.assertEqual(bodies[0]["media"]["media_ids"], ["media-9"])
            self.assertNotIn("media", bodies[1])

    def test_upload_failure_still_posts_the_thread_text_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            photo = self._photo(job_dir)
            _write_thread(job_dir, ["first", "second"], photo_path=str(photo))

            responses = self._tweet_responses(["id1", "id2"])
            with mock.patch.object(xp, "_upload_media", side_effect=RuntimeError("403 forbidden")):
                with mock.patch("requests.post", side_effect=responses) as post:
                    payload = xp.post_thread(job_dir)

            self.assertTrue(payload["posted"])
            self.assertNotIn("media", post.call_args_list[0].kwargs["json"])

    def test_no_photo_path_means_no_upload_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            _write_thread(job_dir, ["first"])
            with mock.patch.object(xp, "_upload_media", side_effect=AssertionError("no upload")):
                with mock.patch("requests.post", side_effect=self._tweet_responses(["id1"])):
                    xp.post_thread(job_dir)

    def test_missing_photo_file_is_skipped_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            _write_thread(job_dir, ["first"], photo_path=str(job_dir / "gone.png"))
            with mock.patch.object(xp, "_upload_media", side_effect=AssertionError("no upload")):
                with mock.patch("requests.post", side_effect=self._tweet_responses(["id1"])):
                    payload = xp.post_thread(job_dir)
            self.assertTrue(payload["posted"])

    def test_resume_does_not_re_upload_the_card(self):
        # On resume the lead tweet is already live; uploading again would
        # attach the card to a mid-thread reply instead.
        with tempfile.TemporaryDirectory() as tmp:
            job_dir = Path(tmp)
            photo = self._photo(job_dir)
            _write_thread(job_dir, ["first", "second"], photo_path=str(photo), tweet_ids=["id1"])

            with mock.patch.object(xp, "_upload_media", side_effect=AssertionError("no upload")):
                with mock.patch("requests.post", side_effect=self._tweet_responses(["id2"])) as post:
                    payload = xp.post_thread(job_dir)

            self.assertEqual(payload["tweet_ids"], ["id1", "id2"])
            self.assertNotIn("media", post.call_args_list[0].kwargs["json"])


class UploadMediaTests(unittest.TestCase):
    def _response(self, body):
        res = mock.Mock()
        res.raise_for_status = mock.Mock()
        res.json.return_value = body
        return res

    def test_reads_media_id_from_v2_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "card.png"
            path.write_bytes(b"png")
            with mock.patch("requests.post", return_value=self._response({"data": {"id": "77"}})):
                self.assertEqual(xp._upload_media(path, access_token="t"), "77")

    def test_accepts_the_legacy_media_id_string_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "card.png"
            path.write_bytes(b"png")
            with mock.patch("requests.post", return_value=self._response({"media_id_string": "88"})):
                self.assertEqual(xp._upload_media(path, access_token="t"), "88")

    def test_unrecognized_response_raises_rather_than_keyerror(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "card.png"
            path.write_bytes(b"png")
            with mock.patch("requests.post", return_value=self._response({"unexpected": 1})):
                with self.assertRaises(RuntimeError):
                    xp._upload_media(path, access_token="t")


if __name__ == "__main__":
    unittest.main()
