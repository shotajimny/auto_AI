"""HTTP/1.1 transport for the official Procon 37 server."""

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


class APIError(Exception):
    def __init__(self, message, status=502, details=None):
        super().__init__(message)
        self.status = status
        self.details = details


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward a competition token to a different endpoint.
        return None


class CompetitionAPI:
    """Serialize requests and leave a 250 ms gap (Q&A 2, Q42)."""

    def __init__(self, url, token, timeout=5):
        if not isinstance(url, str) or not isinstance(token, str):
            raise ValueError("接続先URLとトークンを文字列で指定してください。")
        parts = urllib.parse.urlsplit(url.strip())
        if (parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment):
            raise ValueError("接続先は認証情報・クエリを含まない http(s) URLにしてください。")
        try:
            parts.port
        except ValueError as exc:
            raise ValueError("接続先のポート番号が不正です。") from exc
        if not token.strip() or any(c in token for c in "\r\n"):
            raise ValueError("有効な Procon-Token を入力してください。")
        self.url = url.strip().rstrip("/")
        self._token = token.strip()
        self.timeout = timeout
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), _NoRedirect())
        self._lock = threading.Lock()
        self._last_request = 0.0

    def request(self, method, path, payload=None, deadline=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Procon-Token": self._token, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.url + path, data=data,
                                         headers=headers, method=method)
        with self._lock:
            time.sleep(max(0, 0.25 - (time.monotonic() - self._last_request)))
            timeout = self.timeout
            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0.75:
                    raise APIError("回答締切が近いため送信を中止しました。最新の日を取得して再計画してください。", 409)
                timeout = min(timeout, remaining - 0.25)
            self._last_request = time.monotonic()
            try:
                with self._opener.open(request, timeout=timeout) as response:
                    raw = response.read(4_000_001)
            except urllib.error.HTTPError as exc:
                try:
                    raw = exc.read(16_384)
                finally:
                    exc.close()
                try:
                    details = json.loads(raw)
                    message = details.get("message", str(details)) if isinstance(details, dict) else str(details)
                except (ValueError, UnicodeError):
                    details = None
                    message = raw.decode("utf-8", errors="replace").strip()
                message = message.replace(self._token, "***")
                raise APIError(f"競技サーバー HTTP {exc.code}: {message or exc.reason}",
                               status=502, details={"upstreamStatus": exc.code, "response": details}) from None
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                suffix = " 提出の受理状況は不明です。自動再送はしません。" if method == "POST" else ""
                # Some URL errors echo their request; never expose the token.
                message = str(exc).replace(self._token, "***")
                raise APIError(f"競技サーバーに接続できません: {message}.{suffix}") from None
        if len(raw) > 4_000_000:
            raise APIError("競技サーバーの応答が大きすぎます。")
        if not raw.strip():
            return None  # POST /agent returns an empty 200 response.
        try:
            return json.loads(raw)
        except (ValueError, UnicodeError):
            raise APIError("競技サーバーからJSON以外の応答が返りました。") from None

    def setting(self):
        return self.request("GET", "/setting")

    def state(self):
        return self.request("GET", "/")

    def agents(self, kinds):
        return self.request("POST", "/agent", kinds)

    def submit(self, actions, deadline=None):
        response = self.request("POST", "/", actions, deadline=deadline)
        revision = response.get("revision") if isinstance(response, dict) else None
        if type(revision) is not int:
            raise APIError("提出応答に有効な revision がありません。受理状況を確認してください。")
        if revision < 0:
            raise APIError("行動計画は競技サーバーに拒否されました（revision < 0）。",
                           status=422, details=response)
        return response
