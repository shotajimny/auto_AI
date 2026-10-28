"""Server-side autonomous match loop, independent of browser polling."""

import copy
import threading
import time

from .api import APIError
from .game import normalize_setting, normalize_state
from .planner import choose_agent_kinds


class Autopilot:
    def __init__(self, session):
        self.session = session
        self._event = threading.Event()
        self._thread = None
        self.running = False
        self.phase = "idle"
        self.message = "自動運転を開始すると、最終日まで計画と提出を繰り返します。"
        self.error = None
        self.events = []
        self.days = []
        self.last_plan = None
        self.auto_kinds = True
        self.search_seconds = 0.8
        self._event_id = 0
        self._kinds_done = False
        self._last_ends_at = None
        self._failures = 0
        self._last_setting_poll = 0

    def snapshot(self):
        return copy.deepcopy({"running": self.running, "phase": self.phase,
                              "message": self.message, "error": self.error,
                              "events": self.events, "days": self.days,
                              "lastPlan": self.last_plan, "autoKinds": self.auto_kinds,
                              "searchSeconds": self.search_seconds})

    def reset(self):
        self.phase = "idle"
        self.message = "自動運転を開始すると、最終日まで計画と提出を繰り返します。"
        self.error, self.days, self.events, self.last_plan = None, [], [], None
        self._last_ends_at = None

    def _status(self, phase, message, level="info"):
        if (phase, message) != (self.phase, self.message):
            self._event_id += 1
            self.events.append({"id": self._event_id, "time": int(time.time()),
                                "message": message, "level": level})
            self.events = self.events[-60:]
        self.phase, self.message = phase, message

    def start(self, auto_kinds=True, search_seconds=0.8):
        with self.session.lock:
            self.session._require_setting()
            if self.running:
                return self.session.snapshot()
            if self._thread is not None and self._thread.is_alive():
                raise APIError("前の自動運転を停止しています。少し待って再開してください。", 409)
            if self.session.finished:
                raise ValueError("試合は終了しています。新しい練習マップを読み込んでください。")
            if type(auto_kinds) is not bool:
                raise ValueError("種別の自動選択はtrueまたはfalseで指定してください。")
            if (type(search_seconds) not in (int, float) or not 0.05 <= search_seconds <= 3):
                raise ValueError("自動運転の探索時間は0.05〜3秒で指定してください。")
            self.auto_kinds, self.search_seconds = auto_kinds, search_seconds
            self.error, self._failures = None, 0
            if self.session.state is not None:
                self._last_ends_at = self.session.state["endsAt"]
            self._kinds_done = not auto_kinds or self.session.state is not None and (
                self.session.mode == "live" or self.session.state["day"] > 0 or bool(self.session.history))
            self._event = threading.Event()
            self.running = True
            self._status("starting", "自動運転を開始しました。")
            self._thread = threading.Thread(target=self._run, args=(self._event,),
                                            name="hexa-autopilot", daemon=True)
            self._thread.start()
            return self.session.snapshot()

    def stop(self):
        # Signal before taking the session lock, so an active search can stop.
        self._event.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=7)
        with self.session.lock:
            if self.running:
                self.running = False
                self._status("stopped", "自動運転を停止しました。受理済みの回答は維持されます。")
            return self.session.snapshot()

    def _finish(self):
        # No final state is exposed by the official API after match completion.
        self.session._record_completed(len(self.session.setting["daySteps"]))
        self.session.finished = True
        self.running = False
        self._status("completed", "最終日までの自動運転が完了しました。")

    def _poll_live(self):
        session = self.session
        if session.state is None and time.monotonic() - self._last_setting_poll >= 2:
            try:
                session.setting = normalize_setting(session.api.setting())
            except APIError as exc:
                if (exc.details or {}).get("upstreamStatus") != 403:
                    raise
            self._last_setting_poll = time.monotonic()
        try:
            state = normalize_state(session.setting, session.api.state())
        except APIError as exc:
            upstream = (exc.details or {}).get("upstreamStatus")
            if upstream == 403:
                last_day = len(session.setting["daySteps"]) - 1
                if last_day in session.accepted and self._last_ends_at and time.time() >= self._last_ends_at:
                    self._finish()
                    return
                session.state = None
                session.state_error = None
                self._status("waiting", "試合開始または次の日の状態公開を待っています。")
                return
            raise
        session._apply_state(state)
        self._last_ends_at = state["endsAt"]

    def step(self, event=None):
        """One serialized iteration; separated for deterministic regressions."""
        event = event or self._event
        session = self.session
        if event.is_set() or not self.running:
            return
        if not self._kinds_done:
            self._status("choosing", "巡回車と補給車の役割を自動選択しています。")
            kinds = choose_agent_kinds(session.setting)
            if event.is_set():
                return
            try:
                session.set_agents(kinds, automatic=True)
            except APIError as exc:
                if (exc.details or {}).get("upstreamStatus") != 403:
                    raise
                self._status("waiting", "種別の受付は終了しています。サーバー上の種別で続行します。")
            self._kinds_done = True
        if session.mode == "live":
            self._poll_live()
        if not self.running or session.state is None:
            return
        if session.finished:
            self._finish()
            return
        day = session.state["day"]
        if session.mode == "live" and day in session.accepted:
            self._status("waiting_next_day", f"DAY {day + 1} の回答は受理済みです。次の日を待っています。")
            return
        self._status("planning", f"DAY {day + 1} の行動計画を探索・検証しています。")
        budget = self.search_seconds
        if session.mode == "live":
            budget = max(0, min(budget, session.state["endsAt"] - time.time() - 1.35))
        plan = session.plan(time_budget=budget, cancelled=event.is_set)
        if event.is_set():
            return
        if not plan["validation"]["valid"]:
            raise ValueError("自動生成した計画の検証に失敗しました。")
        self.last_plan = {**plan, "day": day, "startAgents": copy.deepcopy(session.state["agents"])}
        if session.mode == "live":
            self._status("submitting", f"DAY {day + 1} の回答を提出しています。")
            result = session.submit(plan["actions"], day, automatic=True, cancel_event=event)
            revision = result["revision"]
        else:
            session.advance(plan["actions"], automatic=True)
            revision = None
        score = plan["validation"]["score"]
        self.days.append({"day": day, "revision": revision, "score": score,
                          "actions": plan["actions"], "candidates": plan["candidates"],
                          "elapsedMs": plan["elapsedMs"]})
        detail = f"受理 revision {revision}" if revision is not None else "練習を実行"
        self._status("waiting_next_day", f"DAY {day + 1}: {detail}。獲得予測 {score['dailyBrandCount']} 系列・{score['bowls']} 杯。")
        if session.finished:
            self._finish()

    def _run(self, event):
        try:
            while not event.is_set():
                try:
                    with self.session.lock:
                        self.step(event)
                        if not self.running:
                            return
                        self._failures = 0
                except APIError as exc:
                    if event.is_set():
                        return
                    with self.session.lock:
                        upstream = (exc.details or {}).get("upstreamStatus")
                        if exc.status == 409:
                            self._status("waiting", str(exc))
                        elif (exc.method == "POST" or
                              (exc.method is None and self.phase in ("submitting", "choosing")) or
                              upstream in (400, 401, 403) or exc.status == 422):
                            # Ambiguous POST results must never be auto-retried.
                            raise
                        else:
                            self._failures += 1
                            if self._failures >= 5:
                                raise
                            self._status("reconnecting", "状態取得に失敗しました。間隔を空けて再接続しています。")
                if event.wait(1.0 + min(self._failures, 4)):
                    return
        except Exception as exc:
            with self.session.lock:
                self.error = str(exc)
                self.running = False
                self._status("error", f"自動運転を停止しました: {exc}", "error")
        finally:
            with self.session.lock:
                if event is self._event and self.running:
                    self.running = False
                    self._status("stopped", "自動運転を停止しました。受理済みの回答は維持されます。")
