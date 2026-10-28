"""Opt-in compatibility tests using the organizer's supplied executable.

HEXA_OFFICIAL_SERVER=/absolute/path/procon-server-darwin-arm64 \
    python3 -m unittest discover -s tests -p test_official_integration.py -v

The executable is not downloaded or bundled. Tests launch it on loopback,
use temporary synthetic maps/test-only tokens, and stop their child process.
Allow roughly 30 seconds. All tests skip when the environment variable is unset.
"""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest

from hexa_client.api import APIError, CompetitionAPI
from hexa_client.demo import demo_config
from hexa_client.game import normalize_setting, normalize_state, simulate
from hexa_client.planner import plan_actions
from hexa_client.session import Session


OFFICIAL_SERVER = os.environ.get("HEXA_OFFICIAL_SERVER")
TEST_TOKEN = "hexa-integration-test-only"
ROOT = Path(__file__).resolve().parents[1]


@contextmanager
def official_server(config, kind_deadline="2s", start_delay="1s"):
    executable = Path(OFFICIAL_SERVER).expanduser().resolve()
    if not executable.is_file():
        raise AssertionError(f"HEXA_OFFICIAL_SERVER does not exist: {executable}")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="hexa-official-") as directory:
        path = Path(directory)
        (path / "config.json").write_text(json.dumps(config), encoding="utf-8")
        with (path / "server.log").open("w+", encoding="utf-8") as log:
            process = subprocess.Popen(
                [str(executable), "-config", str(path / "config.json"),
                 "-addr", f"127.0.0.1:{port}", "-kind-deadline", kind_deadline,
                 "-match-start-delay", start_delay],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            api = CompetitionAPI(f"http://127.0.0.1:{port}", TEST_TOKEN, timeout=2)
            try:
                deadline = time.monotonic() + 5
                while True:
                    if process.poll() is not None:
                        log.seek(0)
                        raise AssertionError(f"Official server exited: {log.read()}")
                    try:
                        api.setting()
                        break
                    except APIError:
                        if time.monotonic() >= deadline:
                            log.seek(0)
                            raise AssertionError(f"Official server did not become ready: {log.read()}")
                        time.sleep(0.05)
                yield api
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def wait_for_day(api, day, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = api.state()
        except APIError as error:
            upstream = (error.details or {}).get("upstreamStatus")
            if upstream != 403:
                raise
        else:
            if state["day"] == day:
                return state
            if state["day"] > day:
                raise AssertionError(f"Missed day {day}: got {state['day']}")
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for day {day}")


def practice_config(seconds=(4, 4)):
    config = demo_config()
    config["teams"] = [{"name": "integration", "token": TEST_TOKEN}]
    config["problem"]["daySteps"] = [36] * len(seconds)
    config["problem"]["daySeconds"] = list(seconds)
    # Keep synthetic spots consistent with the official flat-cell rule.
    problem = config["problem"]
    for spot in problem["spots"]:
        y, x = divmod(spot["pos"], problem["width"])
        problem["cells"][y][x] = 0
    return config


@unittest.skipUnless(OFFICIAL_SERVER, "Set HEXA_OFFICIAL_SERVER to run official-server tests")
class OfficialServerIntegrationTests(unittest.TestCase):
    def test_background_solver_selects_kinds_and_completes_match_without_browser(self):
        with official_server(practice_config(seconds=(4, 4)), kind_deadline="3s") as api:
            session = Session()
            session.connect(api.url, TEST_TOKEN)
            try:
                session.autopilot.start(search_seconds=0.3)
                session.autopilot._thread.join(timeout=18)
                self.assertFalse(session.autopilot._thread.is_alive())
                result = session.snapshot()
                self.assertEqual(result["autopilot"]["phase"], "completed", result["autopilot"]["error"])
                self.assertTrue(result["finished"])
                self.assertEqual([d["day"] for d in result["autopilot"]["days"]], [0, 1])
                self.assertTrue(all(d["revision"] >= 0 for d in result["autopilot"]["days"]))
                self.assertEqual(result["kinds"].count(1), 1)
                self.assertEqual(len(result["history"]), 2)
                self.assertNotIn(TEST_TOKEN, str(result))
            finally:
                session.autopilot.stop()

    def test_http_planner_acceptance_rejection_and_next_day_state(self):
        """Validate a generated answer against the independent official engine."""
        with official_server(practice_config()) as api:
            setting = normalize_setting(api.setting())
            self.assertEqual(setting["daySeconds"], [4, 4])
            with self.assertRaises(APIError) as before:
                api.state()
            self.assertEqual(before.exception.details["upstreamStatus"], 403)
            self.assertIsNone(api.agents([0, 0, 0, 1]))  # Empty success response.
            state = normalize_state(setting, wait_for_day(api, 0))
            self.assertEqual([a["kind"] for a in state["agents"]], [0, 0, 0, 1])
            actions = plan_actions(setting, state)
            expected = simulate(setting, state, actions, include_trace=False)
            self.assertTrue(expected["valid"], expected["errors"])
            accepted = api.submit(actions)
            self.assertGreaterEqual(accepted["revision"], 0)
            # An invalid replacement is HTTP 200 with a negative revision.
            with self.assertRaises(APIError) as rejected:
                api.submit([[9], [-36], [-36], [-36]])
            self.assertEqual(rejected.exception.status, 422)
            # A rejected replacement must leave the last valid answer active.
            next_day = normalize_state(setting, wait_for_day(api, 1))
            self.assertEqual(next_day["agents"], expected["agents"])
            next_actions = plan_actions(setting, next_day)
            self.assertTrue(simulate(setting, next_day, next_actions)["valid"])
            next_accepted = api.submit(next_actions)
            self.assertGreater(next_accepted["revision"], accepted["revision"])

    def test_cli_automatically_submits_every_day(self):
        """Exercise the shipping CLI, transport, session and planner together."""
        with official_server(practice_config(seconds=(3, 3)), kind_deadline="3s") as api:
            env = dict(os.environ, HEXA_TEST_PROCON_TOKEN=TEST_TOKEN)
            command = [sys.executable, "-m", "hexa_client", "play", "--url", api.url,
                       "--token-env", "HEXA_TEST_PROCON_TOKEN", "--kinds", "0,0,0,1",
                       "--poll", "0.5"]
            result = subprocess.run(command, cwd=ROOT, env=env, text=True,
                                    capture_output=True, timeout=18)
            self.assertEqual(result.returncode, 0, result.stderr)
            submissions = [json.loads(line) for line in result.stdout.splitlines() if line]
            self.assertEqual([item["day"] for item in submissions], [0, 1])
            self.assertTrue(all(item["revision"] >= 0 for item in submissions))
            self.assertNotIn(TEST_TOKEN, result.stdout + result.stderr)

    def test_fuel_can_be_supplied_before_multistep_move_completes(self):
        """Start-time fuel may be insufficient; arrival-time fuel is decisive."""
        cells = [[0, 2, 0, 0, 0, 0, 0, 0]] + [[0] * 8 for _ in range(7)]
        config = {"problem": {
            "width": 8, "height": 8, "cells": cells,
            "spots": [{"brand": 0, "pos": 3, "stocks": 2}],
            "agentStarts": [0, 2], "fuelLimits": 2,
            "daySteps": [3, 3, 3], "daySeconds": [2, 2, 2],
            "busyThreshold": 2, "jammedThreshold": 4,
        }, "teams": [{"token": TEST_TOKEN}]}
        with official_server(config, kind_deadline="1s") as api:
            setting = normalize_setting(api.setting())
            api.agents([0, 1])
            wait_for_day(api, 0)
            api.submit([[2, -1], [-3]])
            state = normalize_state(setting, wait_for_day(api, 1))
            self.assertEqual(state["agents"][0], {"kind": 0, "pos": 1, "fuel": 1})
            actions = [[2], [5, -1]]
            expected = simulate(setting, state, actions, include_trace=False)
            self.assertTrue(expected["valid"], expected["errors"])
            api.submit(actions)
            next_day = normalize_state(setting, wait_for_day(api, 2))
            self.assertEqual(next_day["agents"], expected["agents"])
            self.assertEqual(next_day["agents"][0], {"kind": 0, "pos": 2, "fuel": 0})


    def test_traffic_uses_position_after_move_reflection(self):
        """The binary and supplement table differ from Q&A 2 Q27 wording."""
        cells = [[0] * 8 for _ in range(8)]
        cells[0][0] = 1
        config = {"problem": {
            "width": 8, "height": 8, "cells": cells,
            "spots": [{"brand": 0, "pos": 1, "stocks": 1}],
            "agentStarts": [0], "fuelLimits": 10,
            "daySteps": [3, 3], "daySeconds": [2, 2],
            "busyThreshold": 1, "jammedThreshold": 2,
        }, "teams": [{"token": TEST_TOKEN}]}
        with official_server(config, kind_deadline="1s") as api:
            setting = normalize_setting(api.setting())
            api.agents([0])
            state = normalize_state(setting, wait_for_day(api, 0))
            actions = [[2, -2]]  # Leave the sole road at step 1.
            predicted = simulate(setting, state, actions, include_trace=False)
            self.assertTrue(predicted["valid"], predicted["errors"])
            self.assertEqual(predicted["trafficCounts"], [{"pos": 0, "count": 0}])
            api.submit(actions)
            next_day = normalize_state(setting, wait_for_day(api, 1))
            self.assertEqual(next_day["traffics"], [{"pos": 0, "status": 0}])


if __name__ == "__main__":
    unittest.main()
