import copy
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from hexa_client.api import APIError
from hexa_client.__main__ import auto_play
from hexa_client.demo import demo_config
from hexa_client.game import initial_state, normalize_setting
from hexa_client.session import Session


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.session = Session()
        self.session.demo()
        self.actions = [[-self.session.setting["daySteps"][0]]
                        for _ in self.session.state["agents"]]

    def live(self):
        api = Mock()
        api.url = "http://127.0.0.1:8080"
        api.state.return_value = copy.deepcopy(self.session.state)
        api.submit.return_value = {"revision": 1}
        self.session.mode = "live"
        self.session.api = api
        return api

    def test_demo_config_is_normalized_and_contains_no_credentials(self):
        snapshot = self.session.snapshot()
        self.assertNotIn("teams", snapshot["setting"])
        self.assertNotIn("token", str(snapshot))
        self.assertEqual(snapshot["mode"], "demo")
        self.assertEqual(snapshot["state"]["day"], 0)

    def test_kind_changes_validate_exact_agent_count(self):
        with self.assertRaises(ValueError):
            self.session.set_agents([0])
        snapshot = self.session.set_agents([0, 0, 0, 1])
        self.assertEqual(snapshot["state"]["agents"][-1]["kind"], 1)

    def test_offline_progress_updates_day_and_carries_fuel(self):
        result = self.session.advance(self.actions)
        self.assertEqual(result["state"]["day"], 1)
        self.assertEqual(len(result["history"]), 1)
        with self.assertRaises(ValueError):
            self.session.set_agents([0, 0, 0, 1])

    def test_practice_last_day_finishes(self):
        for steps in self.session.setting["daySteps"]:
            self.session.advance([[-steps] for _ in self.session.state["agents"]])
        self.assertTrue(self.session.finished)
        with self.assertRaises(ValueError):
            self.session.plan()

    def test_stale_day_cannot_be_submitted(self):
        api = self.live()
        with self.assertRaises(APIError) as cm:
            self.session.submit(self.actions, 1)
        self.assertEqual(cm.exception.status, 409)
        api.submit.assert_not_called()

    def test_day_rollover_between_edit_and_submit_never_posts(self):
        api = self.live()
        api.state.return_value["day"] = 1
        with self.assertRaises(APIError) as cm:
            self.session.submit(self.actions, 0)
        self.assertEqual(cm.exception.status, 409)
        self.assertEqual(self.session.state["day"], 1)
        api.submit.assert_not_called()

    def test_submit_rollover_commits_previous_accepted_brands_once(self):
        api = self.live()
        self.session.submit(self.actions, 0)
        self.session.accepted[0]["validation"]["score"]["dailyBrands"] = [3, 7]
        api.state.return_value["day"] = 1
        api.submit.reset_mock()
        with self.assertRaises(APIError) as cm:
            self.session.submit(self.actions, 0)
        self.assertEqual(cm.exception.status, 409)
        api.submit.assert_not_called()
        self.assertEqual(self.session.known_brands, {3, 7})
        self.assertEqual([item["day"] for item in self.session.history], [0])
        self.assertTrue(self.session.accepted[0]["recorded"])
        api.setting.return_value = copy.deepcopy(self.session.setting)
        self.session.refresh()
        self.assertEqual(len(self.session.history), 1)

    def test_invalid_action_never_reaches_server(self):
        api = self.live()
        with self.assertRaises(APIError) as cm:
            self.session.submit([[9], *self.actions[1:]], 0)
        self.assertEqual(cm.exception.status, 422)
        api.submit.assert_not_called()

    def test_rejected_replacement_keeps_accepted_plan(self):
        api = self.live()
        self.session.submit(self.actions, 0)
        first = copy.deepcopy(self.session.accepted[0])
        api.submit.side_effect = APIError("rejected", 422)
        with self.assertRaises(APIError):
            self.session.submit(self.actions, 0)
        self.assertEqual(self.session.accepted[0], first)

    def test_connect_pre_match_keeps_setting_and_exposes_state_error(self):
        api = Mock()
        api.url = "http://localhost:8080"
        api.setting.return_value = normalize_setting(demo_config())
        api.state.side_effect = APIError("match has not started")
        with patch("hexa_client.session.CompetitionAPI", return_value=api):
            snapshot = self.session.connect("http://localhost:8080", "test-token")
        self.assertIsNotNone(snapshot["setting"])
        self.assertIsNone(snapshot["state"])
        self.assertIn("not started", snapshot["stateError"])


class AutoPlayBoundaryTests(unittest.TestCase):
    def fake_session(self):
        session = Mock()
        session.state = {"day": 0}
        session.state_error = None
        session.setting = {"daySteps": [8, 8]}
        session.plan.return_value = {"actions": [[-8]]}
        return session

    def arguments(self):
        return SimpleNamespace(token_env="HEXA_TEST_TOKEN", url="http://localhost:8080",
                               kinds=None, poll=0.5, once=False)

    def test_pre_post_conflict_refreshes_and_submits_next_day(self):
        session = self.fake_session()
        session.submit.side_effect = [APIError("day changed before POST", 409),
                                      {"revision": 2}]
        session.refresh.side_effect = lambda: setattr(session, "state", {"day": 1})
        output = io.StringIO()
        with patch("hexa_client.__main__.Session", return_value=session), \
                patch.dict("os.environ", {"HEXA_TEST_TOKEN": "test-only"}), \
                patch("hexa_client.__main__.time.sleep"), \
                patch("sys.stdout", output), patch("sys.stderr", io.StringIO()):
            self.assertEqual(auto_play(self.arguments()), 0)
        session.refresh.assert_called_once()
        self.assertEqual([call.args[1] for call in session.submit.call_args_list], [0, 1])
        self.assertEqual([json.loads(line)["day"] for line in output.getvalue().splitlines()], [1])

    def test_ambiguous_post_failure_is_never_retried(self):
        session = self.fake_session()
        session.submit.side_effect = APIError("POST acceptance is unknown", 502)
        with patch("hexa_client.__main__.Session", return_value=session), \
                patch.dict("os.environ", {"HEXA_TEST_TOKEN": "test-only"}):
            with self.assertRaises(APIError):
                auto_play(self.arguments())
        session.submit.assert_called_once()
        session.refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
