"""Session coordination shared by the GUI and CLI."""

import copy
import threading
import time

from .api import APIError, CompetitionAPI
from .autopilot import Autopilot
from .demo import demo_config
from .game import initial_state, normalize_setting, normalize_state, simulate
from .planner import solve


class Session:
    def __init__(self):
        self.lock = threading.RLock()
        self.mode = "disconnected"
        self.api = None
        self.setting = None
        self.state = None
        self.state_error = None
        self.kinds = []
        self.known_brands = set()
        self.accepted = {}
        self.history = []
        self.finished = False
        self.autopilot = Autopilot(self)

    def snapshot(self):
        return copy.deepcopy({
            "mode": self.mode, "endpoint": self.api.url if self.api else "",
            "setting": self.setting, "state": self.state, "stateError": self.state_error,
            "kinds": self.kinds, "knownBrands": sorted(self.known_brands),
            "history": self.history, "finished": self.finished,
            "autopilot": self.autopilot.snapshot(),
            "notice": ("練習では他チームは動かず、翌日の道路状態は固定です。得点は予測値です。"
                       if self.mode in ("demo", "offline") else
                       "収集結果はローカル予測です。受理と競技上の有効性はサーバーの応答を確認してください。"),
        })

    def _reset(self):
        self.known_brands = set()
        self.accepted = {}
        self.history = []
        self.state_error = None
        self.finished = False
        self.autopilot.reset()

    def _require_manual_control(self, automatic=False):
        if self.autopilot.running and not automatic:
            raise APIError("自動運転中です。手動で変更する前に自動運転を停止してください。", 409)

    def import_config(self, config, mode="offline"):
        setting = normalize_setting(config)
        with self.lock:
            self._require_manual_control()
            self._reset()
            self.mode, self.api, self.setting = mode, None, setting
            self.kinds = [0] * len(setting["agents"])
            self.state = initial_state(setting, self.kinds)
            self.state["endsAt"] = int(time.time()) + setting["daySeconds"][0]
            return self.snapshot()

    def demo(self):
        return self.import_config(demo_config(), mode="demo")

    def connect(self, url, token):
        with self.lock:
            self._require_manual_control()
            api = CompetitionAPI(url, token)
            setting = normalize_setting(api.setting())
            self._reset()
            self.mode, self.api, self.setting = "live", api, setting
            self.kinds = [0] * len(setting["agents"])
            self.state = None
            self._fetch_state()
            return self.snapshot()

    def _fetch_state(self):
        try:
            state = normalize_state(self.setting, self.api.state())
            self._apply_state(state)
        except APIError as exc:
            self.state = None
            self.state_error = str(exc)

    def _apply_state(self, state):
        # Both polling and the pre-submit freshness check can discover a new
        # day. Commit the latest accepted prior-day prediction in either case,
        # before a caller can plan using the new positions and fuel.
        self._record_completed(state["day"])
        self.state = state
        self.kinds = [a["kind"] for a in state["agents"]]
        self.state_error = None

    def _record_completed(self, current_day):
        for day in sorted(self.accepted):
            if day < current_day and not self.accepted[day].get("recorded"):
                item = self.accepted[day]
                self.known_brands.update(item["validation"]["score"]["dailyBrands"])
                self.history.append({"day": day, "revision": item["revision"],
                                     "score": item["validation"]["score"], "predicted": True})
                item["recorded"] = True

    def refresh(self):
        with self.lock:
            if self.autopilot.running or self.finished:
                return self.snapshot()
            if self.mode == "live":
                try:
                    self.setting = normalize_setting(self.api.setting())
                except APIError as exc:
                    self.state = None
                    self.state_error = str(exc)
                    return self.snapshot()
                self._fetch_state()
            return self.snapshot()

    def set_agents(self, kinds, automatic=False):
        with self.lock:
            self._require_manual_control(automatic)
            self._require_setting()
            if (not isinstance(kinds, list) or len(kinds) != len(self.setting["agents"])
                    or any(type(k) is not int or k not in (0, 1) for k in kinds)):
                raise ValueError("種別はエージェント数と同じ長さの 0 / 1 の配列で指定してください。")
            if self.mode == "live":
                self.api.agents(kinds)
            else:
                if self.state["day"] != 0 or self.history:
                    raise ValueError("練習の種別変更は初日の開始前に行ってください。")
                self.state = initial_state(self.setting, kinds)
                self.state["endsAt"] = int(time.time()) + self.setting["daySeconds"][0]
            self.kinds = list(kinds)
            return self.snapshot()

    def _require_setting(self):
        if self.setting is None:
            raise ValueError("競技サーバーに接続するか、練習マップを読み込んでください。")

    def _require_state(self):
        self._require_setting()
        if self.state is None or self.state_error:
            raise ValueError("当日の試合状態を取得できていません。試合開始後に更新してください。")
        if self.finished:
            raise ValueError("練習は終了しました。新しいマップを読み込んでください。")

    def validate(self, actions):
        with self.lock:
            self._require_state()
            return simulate(self.setting, self.state, actions, self.known_brands)

    def plan(self, time_budget=0.8, cancelled=None):
        with self.lock:
            self._require_state()
            return solve(self.setting, self.state, self.known_brands,
                         time_budget=time_budget, cancelled=cancelled)

    def submit(self, actions, day, automatic=False, cancel_event=None):
        with self.lock:
            self._require_manual_control(automatic)
            self._require_state()
            if self.mode != "live":
                raise ValueError("練習モードでは提出できません。検証または翌日への進行を利用してください。")
            if type(day) is not int or day != self.state["day"]:
                raise APIError("編集していた日と現在の日が一致しません。状態を更新して再計画してください。", 409)
            # Re-read the actual day immediately before validation and POST.
            current = normalize_state(self.setting, self.api.state())
            if current != self.state:
                self._apply_state(current)
                raise APIError("試合状態が変化しました。更新した状態で再計画してください。", 409)
            validation = self.validate(actions)
            if not validation["valid"]:
                raise APIError("行動計画が不正です。検証結果を修正してください。", 422, validation)
            if cancel_event is not None and cancel_event.is_set():
                raise APIError("自動運転が停止されたため提出を中止しました。", 409)
            options = {"deadline": current["endsAt"]}
            if cancel_event is not None:
                options["cancel_event"] = cancel_event
            response = self.api.submit(actions, **options)
            self.accepted[day] = {**response, "actions": copy.deepcopy(actions),
                                  "validation": validation, "recorded": False}
            return {**response, "day": day, "validation": validation}

    def advance(self, actions, automatic=False):
        with self.lock:
            self._require_manual_control(automatic)
            self._require_state()
            if self.mode not in ("demo", "offline"):
                raise ValueError("翌日への進行は練習モードでのみ利用できます。")
            report = self.validate(actions)
            if not report["valid"]:
                raise APIError("不正な計画では練習を進められません。", 422, report)
            day = self.state["day"]
            self.known_brands.update(report["score"]["dailyBrands"])
            self.history.append({"day": day, "score": report["score"], "predicted": True})
            self.state["agents"] = [{k: a[k] for k in ("kind", "pos", "fuel")}
                                    for a in report["agents"]]
            if day + 1 >= len(self.setting["daySteps"]):
                self.finished = True
            else:
                self.state["day"] += 1
                self.state["endsAt"] = int(time.time()) + self.setting["daySeconds"][day + 1]
            return self.snapshot()
