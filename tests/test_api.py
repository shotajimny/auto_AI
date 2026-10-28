import io
import json
import threading
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

from hexa_client.api import APIError, CompetitionAPI, _NoRedirect


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.api = CompetitionAPI("http://127.0.0.1:8080", "test-token")
        self.api._opener = MagicMock()
        self.sleep = patch("hexa_client.api.time.sleep").start()
        self.addCleanup(patch.stopall)

    def reply(self, content):
        self.api._opener.open.return_value.__enter__.return_value.read.return_value = content

    def test_official_submit_is_raw_array_with_header_auth(self):
        self.reply(b'{"revision": 7}')
        self.assertEqual(self.api.submit([[-10], [-10]]), {"revision": 7})
        request = self.api._opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8080/")
        self.assertEqual(request.get_header("Procon-token"), "test-token")
        self.assertEqual(json.loads(request.data), [[-10], [-10]])
        self.assertEqual(request.method, "POST")

    def test_empty_agent_response_is_success(self):
        self.reply(b"")
        self.assertIsNone(self.api.agents([0, 1]))
        self.assertEqual(self.api._opener.open.call_args.args[0].full_url,
                         "http://127.0.0.1:8080/agent")

    def test_negative_revision_is_rejected(self):
        self.reply(b'{"revision": -1}')
        with self.assertRaises(APIError) as cm:
            self.api.submit([[-10]])
        self.assertEqual(cm.exception.status, 422)

    def test_plain_text_http_errors_are_readable(self):
        self.api._opener.open.side_effect = urllib.error.HTTPError(
            "http://127.0.0.1:8080/agent", 400, "Bad Request", {}, io.BytesIO(b"wrong kinds length"))
        with self.assertRaisesRegex(APIError, "wrong kinds length") as cm:
            self.api.agents([0])
        self.assertEqual(cm.exception.details["upstreamStatus"], 400)

    def test_timed_out_post_is_not_retried(self):
        self.api._opener.open.side_effect = TimeoutError("test-token timeout")
        with self.assertRaises(APIError) as cm:
            self.api.submit([[-10]])
        self.assertEqual(self.api._opener.open.call_count, 1)
        self.assertNotIn("test-token", str(cm.exception))
        self.assertIn("受理状況は不明", str(cm.exception))

    def test_expired_day_is_not_sent_after_rate_limit_wait(self):
        with patch("hexa_client.api.time.time", return_value=100):
            with self.assertRaises(APIError) as cm:
                self.api.submit([[-10]], deadline=100.5)
        self.assertEqual(cm.exception.status, 409)
        self.api._opener.open.assert_not_called()

    def test_stop_after_rate_limit_wait_prevents_post(self):
        stopped = threading.Event()
        self.sleep.side_effect = lambda seconds: stopped.set()
        with self.assertRaises(APIError) as cm:
            self.api.submit([[-10]], cancel_event=stopped)
        self.assertEqual(cm.exception.status, 409)
        self.api._opener.open.assert_not_called()

    def test_redirects_never_forward_tokens(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))

    def test_invalid_urls_and_header_injection(self):
        for url in ["file:///etc/passwd", "http://name:secret@localhost", "http://localhost?token=x",
                    "http://localhost/#fragment", "http://localhost:hello"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                CompetitionAPI(url, "token")
        with self.assertRaises(ValueError):
            CompetitionAPI("http://localhost", "token\nInjected: header")


if __name__ == "__main__":
    unittest.main()
