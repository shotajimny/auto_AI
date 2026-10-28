import copy
import threading
import time
import unittest
from unittest.mock import Mock, patch

from hexa_client.api import APIError
from hexa_client.session import Session


class AutopilotTests(unittest.TestCase):
    def setUp(self):
        self.session = Session()
        self.session.demo()
        self.auto = self.session.autopilot
        self.auto.running = True
        self.auto.search_seconds = 0.05

    def live(self):
        api = Mock()
        api.url = "http://localhost:8080"
        api.state.return_value = copy.deepcopy(self.session.state)
        api.setting.return_value = copy.deepcopy(self.session.setting)
        api.submit.return_value = {"revision": 1}
        self.session.api, self.session.mode = api, "live"
        self.auto._kinds_done = True
        return api

    def test_practice_runs_all_days_without_browser_requests(self):
        for _ in self.session.setting["daySteps"]:
            self.auto.step()
        self.assertTrue(self.session.finished)
        self.assertFalse(self.auto.running)
        self.assertEqual(self.auto.phase, "completed")
        self.assertEqual([d["day"] for d in self.auto.days], [0, 1, 2])
        self.assertTrue(all(d["revision"] is None for d in self.auto.days))
        self.assertEqual(len(self.session.history), 3)
        self.assertEqual(self.session.kinds.count(1), 1)

    def test_finished_route_keeps_its_actual_start_positions(self):
        self.session.state["day"] = len(self.session.setting["daySteps"]) - 1
        self.auto._kinds_done = True
        start = copy.deepcopy(self.session.state["agents"])
        self.auto.step()
        self.assertTrue(self.session.finished)
        self.assertEqual(self.auto.last_plan["startAgents"], start)
        self.assertEqual(self.auto.last_plan["validation"]["agents"], self.session.state["agents"])

    def test_each_live_day_is_submitted_once_even_after_resume(self):
        api = self.live()
        self.auto.step()
        self.auto.step()
        self.assertEqual(api.submit.call_count, 1)
        self.assertEqual(len(self.auto.days), 1)
        self.assertEqual(self.auto.days[0]["revision"], 1)
        self.assertIn(0, self.session.accepted)

    def test_stop_during_planning_never_posts(self):
        api = self.live()
        report = self.session.plan(time_budget=0)
        def planning(**kwargs):
            self.auto._event.set()
            return report
        with patch.object(self.session, "plan", side_effect=planning):
            self.auto.step()
        api.submit.assert_not_called()
        self.assertEqual(self.auto.days, [])

    def test_manual_changes_are_locked_while_running(self):
        for action in [lambda: self.session.demo(),
                       lambda: self.session.connect("http://localhost:8080", "test"),
                       lambda: self.session.set_agents([0, 0, 0, 1]),
                       lambda: self.session.advance([[-36]] * 4),
                       lambda: self.session.submit([[-36]] * 4, 0)]:
            with self.assertRaises(APIError) as cm:
                action()
            self.assertEqual(cm.exception.status, 409)

    def test_refresh_does_not_duplicate_upstream_polling(self):
        api = self.live()
        self.session.refresh()
        api.state.assert_not_called()
        api.setting.assert_not_called()

    def test_finished_match_is_kept_visible_without_post_match_polling(self):
        api = self.live()
        self.auto.running = False
        self.session.finished = True
        state = copy.deepcopy(self.session.state)
        result = self.session.refresh()
        self.assertEqual(result["state"], state)
        api.setting.assert_not_called()
        api.state.assert_not_called()

    def test_pre_match_wait_and_closed_kind_window_are_not_fatal(self):
        api = self.live()
        self.session.state = None
        self.auto._kinds_done = False
        closed = APIError("not started", details={"upstreamStatus": 403})
        api.agents.side_effect = closed
        api.state.side_effect = closed
        self.auto.step()
        self.assertTrue(self.auto.running)
        self.assertEqual(self.auto.phase, "waiting")
        self.assertTrue(self.auto._kinds_done)
        api.submit.assert_not_called()

    def test_unknown_post_result_stops_without_retry(self):
        api = self.live()
        api.submit.side_effect = APIError("受理状況は不明です。")
        self.auto._run(self.auto._event)
        self.assertFalse(self.auto.running)
        self.assertEqual(self.auto.phase, "error")
        self.assertIn("不明", self.auto.error)
        api.submit.assert_called_once()

    def test_read_failure_before_post_reconnects_without_duplicate_submission(self):
        api = self.live()
        state = copy.deepcopy(self.session.state)
        api.state.side_effect = [state, APIError("read timeout", method="GET"), state, state]
        with patch.object(self.auto._event, "wait", side_effect=[False, True]):
            self.auto._run(self.auto._event)
        api.submit.assert_called_once()
        self.assertEqual(len(self.auto.days), 1)
        self.assertIsNone(self.auto.error)

    def test_auth_failure_stops_without_retry(self):
        api = self.live()
        api.state.side_effect = APIError("AuthError", details={"upstreamStatus": 401})
        self.auto._run(self.auto._event)
        self.assertEqual(self.auto.phase, "error")
        self.assertEqual(api.state.call_count, 1)

    def test_server_end_records_final_accepted_prediction(self):
        api = self.live()
        last = len(self.session.setting["daySteps"]) - 1
        self.session.state["day"] = last
        api.state.return_value = copy.deepcopy(self.session.state)
        self.auto.step()
        self.auto._last_ends_at = time.time() - 1
        api.state.side_effect = APIError("match has ended", details={"upstreamStatus": 403})
        self.auto.step()
        self.assertTrue(self.session.finished)
        self.assertEqual(self.auto.phase, "completed")
        self.assertEqual(self.session.history[-1]["day"], last)
        self.assertTrue(self.session.accepted[last]["recorded"])

    def test_start_stop_and_resume_thread(self):
        self.auto.running = False
        self.auto.start(search_seconds=0.05)
        self.auto.stop()
        self.assertFalse(self.auto.running)
        self.assertFalse(self.auto._thread.is_alive())
        before = len(self.session.history)
        if not self.session.finished:
            self.auto.start(search_seconds=0.05)
            self.auto.stop()
            self.assertGreaterEqual(len(self.session.history), before)


if __name__ == "__main__":
    unittest.main()
