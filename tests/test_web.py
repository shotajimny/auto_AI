import http.client
import json
import threading
import unittest

from hexa_client.server import ClientServer


class WebClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ClientServer(("127.0.0.1", 0))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        self.server.session.demo()

    def call(self, path, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            data = None if payload is None else json.dumps(payload).encode()
            all_headers = {"Content-Type": "application/json"} if data else {}
            all_headers.update(headers or {})
            connection.request("GET" if payload is None else "POST", path, data, all_headers)
            response = connection.getresponse()
            body = response.read()
            kind = response.getheader("Content-Type", "")
            return response.status, json.loads(body) if "application/json" in kind else body
        finally:
            connection.close()

    def test_browser_entry_and_static_assets(self):
        status, html = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn(b'lang="ja"', html)
        for path in ("/app.js", "/style.css"):
            self.assertEqual(self.call(path)[0], 200)
        self.assertEqual(self.call("/../api.py")[0], 404)

    def test_generate_validate_and_advance_via_real_http(self):
        status, plan = self.call("/api/plan", {})
        self.assertEqual(status, 200)
        self.assertTrue(plan["validation"]["valid"])
        self.assertGreater(plan["validation"]["score"]["bowls"], 0)
        status, validation = self.call("/api/validate", {"actions": plan["actions"]})
        self.assertEqual(status, 200)
        self.assertTrue(validation["valid"])
        status, session = self.call("/api/advance", {"actions": plan["actions"]})
        self.assertEqual(status, 200)
        self.assertEqual(session["state"]["day"], 1)
        self.assertEqual(session["state"]["agents"], validation["agents"])

    def test_invalid_plan_is_reported_without_advancing(self):
        status, report = self.call("/api/validate", {"actions": [[9]] * 4})
        self.assertEqual(status, 200)
        self.assertFalse(report["valid"])
        self.assertEqual(self.call("/api/advance", {"actions": [[9]] * 4})[0], 422)
        self.assertEqual(self.call("/api/session")[1]["state"]["day"], 0)

    def test_foreign_origin_and_host_are_rejected(self):
        self.assertEqual(self.call("/api/demo", {}, {"Origin": "https://other.example"})[0], 403)
        self.assertEqual(self.call("/api/session", headers={"Host": "other.example"})[0], 403)

    def test_practice_submit_never_claims_server_acceptance(self):
        actions = [[-36]] * 4
        self.assertEqual(self.call("/api/submit", {"actions": actions, "day": 0})[0], 400)

    def test_background_solver_runs_to_completion_without_browser_polling(self):
        status, session = self.call("/api/auto/start", {"searchSeconds": 0.05})
        self.assertEqual(status, 200)
        self.assertTrue(session["autopilot"]["running"])
        self.server.session.autopilot._thread.join(timeout=8)
        result = self.call("/api/session")[1]
        self.assertTrue(result["finished"])
        self.assertEqual(result["autopilot"]["phase"], "completed")
        self.assertEqual(len(result["autopilot"]["days"]), 3)
        self.assertEqual(len(result["history"]), 3)

    def test_auto_start_locks_manual_mutations_and_stop_releases_them(self):
        self.assertEqual(self.call("/api/auto/start", {"searchSeconds": 0.05})[0], 200)
        self.assertEqual(self.call("/api/demo", {})[0], 409)
        status, result = self.call("/api/auto/stop", {})
        self.assertEqual(status, 200)
        self.assertFalse(result["autopilot"]["running"])
        self.assertEqual(self.call("/api/demo", {})[0], 200)


if __name__ == "__main__":
    unittest.main()
