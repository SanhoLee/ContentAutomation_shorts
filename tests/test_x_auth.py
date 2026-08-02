import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "dev" / "src" / "common"))

import x_auth


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_writes_token_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x_token.json"
            x_auth.bootstrap("acc1", "ref1", expires_in=100, token_file=path)
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["access_token"], "acc1")
            self.assertEqual(stored["refresh_token"], "ref1")
            self.assertGreater(stored["expires_at"], time.time())


class GetValidAccessTokenTests(unittest.TestCase):
    def test_missing_token_file_raises_with_bootstrap_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "missing.json"
            with self.assertRaises(RuntimeError) as ctx:
                x_auth.get_valid_access_token(token_file=path)
            self.assertIn("등록", str(ctx.exception))

    def test_unexpired_token_returned_without_refresh_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x_token.json"
            x_auth.bootstrap("acc1", "ref1", expires_in=3600, token_file=path)
            with mock.patch("requests.post", side_effect=AssertionError("should not refresh")):
                token = x_auth.get_valid_access_token(token_file=path)
            self.assertEqual(token, "acc1")

    def test_expired_token_refreshes_and_rotates_refresh_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x_token.json"
            x_auth.bootstrap("stale_acc", "stale_ref", expires_in=-10, token_file=path)

            response = mock.Mock()
            response.raise_for_status = mock.Mock()
            response.json.return_value = {
                "access_token": "fresh_acc",
                "refresh_token": "fresh_ref",
                "expires_in": 7200,
            }
            with mock.patch.dict("os.environ", {"X_CLIENT_ID": "cid", "X_CLIENT_SECRET": "csecret"}):
                with mock.patch("requests.post", return_value=response) as post:
                    token = x_auth.get_valid_access_token(token_file=path)
                    post.assert_called_once()
                    self.assertIn("stale_ref", post.call_args.kwargs["data"]["refresh_token"])

            self.assertEqual(token, "fresh_acc")
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["access_token"], "fresh_acc")
            self.assertEqual(stored["refresh_token"], "fresh_ref")

    def test_missing_client_credentials_raises_before_network_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x_token.json"
            x_auth.bootstrap("stale_acc", "stale_ref", expires_in=-10, token_file=path)
            with mock.patch.dict("os.environ", {}, clear=False):
                import os
                os.environ.pop("X_CLIENT_ID", None)
                os.environ.pop("X_CLIENT_SECRET", None)
                with mock.patch("requests.post", side_effect=AssertionError("should not call network")):
                    with self.assertRaises(RuntimeError):
                        x_auth.get_valid_access_token(token_file=path)

    def test_refresh_response_missing_refresh_token_keeps_old_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x_token.json"
            x_auth.bootstrap("stale_acc", "stale_ref", expires_in=-10, token_file=path)

            response = mock.Mock()
            response.raise_for_status = mock.Mock()
            response.json.return_value = {"access_token": "fresh_acc", "expires_in": 7200}
            with mock.patch.dict("os.environ", {"X_CLIENT_ID": "cid", "X_CLIENT_SECRET": "csecret"}):
                with mock.patch("requests.post", return_value=response):
                    x_auth.get_valid_access_token(token_file=path)

            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(stored["refresh_token"], "stale_ref")

    def test_refresh_failure_propagates_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x_token.json"
            x_auth.bootstrap("stale_acc", "stale_ref", expires_in=-10, token_file=path)
            with mock.patch.dict("os.environ", {"X_CLIENT_ID": "cid", "X_CLIENT_SECRET": "csecret"}):
                with mock.patch("requests.post", side_effect=ConnectionError("boom")) as post:
                    with self.assertRaises(ConnectionError):
                        x_auth.get_valid_access_token(token_file=path)
                    post.assert_called_once()


if __name__ == "__main__":
    unittest.main()
