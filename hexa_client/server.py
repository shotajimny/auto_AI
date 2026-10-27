"""Loopback-only web UI and same-origin competition API bridge."""

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .api import APIError
from .session import Session

STATIC = Path(__file__).with_name("static")


class ClientServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, session=None):
        self.session = session or Session()
        super().__init__(address, ClientHandler)


class ClientHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "HexaUdon/0.1"

    def log_message(self, format, *args):
        pass  # Tokens, upstream URLs and answer bodies never enter access logs.

    def _send(self, status, data, content_type="application/json; charset=utf-8"):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        elif isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _trusted_request(self):
        host = self.headers.get("Host", "")
        try:
            parsed = urlsplit("http://" + host)
            trusted = (parsed.hostname in ("127.0.0.1", "localhost", "::1")
                       and (parsed.port or 80) == self.server.server_port)
        except ValueError:
            trusted = False
        if not trusted:
            self._send(403, {"error": "ローカルホストからアクセスしてください。"})
            self.close_connection = True
            return False
        origin = self.headers.get("Origin")
        if origin and origin != "http://" + host:
            self._send(403, {"error": "異なるオリジンからの操作は受け付けません。"})
            self.close_connection = True
            return False
        return True

    def do_GET(self):
        if not self._trusted_request():
            return
        path = urlsplit(self.path).path
        try:
            if path == "/api/session":
                with self.server.session.lock:
                    self._send(200, self.server.session.snapshot())
            elif path == "/api/refresh":
                self._send(200, self.server.session.refresh())
            elif path in ("/", "/index.html", "/app.js", "/style.css"):
                filename = "index.html" if path == "/" else path[1:]
                data = (STATIC / filename).read_bytes()
                kind = mimetypes.guess_type(filename)[0] or "application/octet-stream"
                self._send(200, data, kind + "; charset=utf-8")
            else:
                self._send(404, {"error": "指定されたページはありません。"})
        except (APIError, ValueError) as exc:
            self._error(exc)

    def do_POST(self):
        if not self._trusted_request():
            return
        try:
            if self.headers.get_content_type() != "application/json":
                raise APIError("Content-Type: application/json を指定してください。", 415)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 1_000_000:
                self.close_connection = True
                raise APIError("JSON本文は1〜1,000,000バイトで指定してください。", 413)
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("API本文はJSONオブジェクトで指定してください。")
            session = self.server.session
            path = urlsplit(self.path).path
            if path == "/api/demo":
                result = session.demo()
            elif path == "/api/import":
                result = session.import_config(payload.get("config"))
            elif path == "/api/connect":
                result = session.connect(payload.get("url"), payload.get("token"))
            elif path == "/api/agents":
                result = session.set_agents(payload.get("kinds"))
            elif path == "/api/plan":
                result = session.plan()
            elif path == "/api/validate":
                result = session.validate(payload.get("actions"))
            elif path == "/api/submit":
                result = session.submit(payload.get("actions"), payload.get("day"))
            elif path == "/api/advance":
                result = session.advance(payload.get("actions"))
            else:
                self._send(404, {"error": "指定されたAPIはありません。"})
                return
            self._send(200, result)
        except (APIError, ValueError, TypeError, KeyError, OverflowError) as exc:
            self._error(exc)

    def _error(self, exc):
        if isinstance(exc, APIError):
            self._send(exc.status, {"error": str(exc), "details": exc.details})
        else:
            self._send(400, {"error": str(exc) or "入力データが不正です。"})
