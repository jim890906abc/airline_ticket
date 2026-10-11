"""網站：標準函式庫的 HTTP server，提供單頁介面與 JSON API。"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .service import Service

log = logging.getLogger("ticket_watch.web")

STATIC = os.path.join(os.path.dirname(__file__), "static")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def make_handler(svc: Service, password: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "TicketWatch"

        def log_message(self, fmt, *args):  # 安靜一點，只記錯誤
            pass

        # ---------------- 基本工具

        def _authorized(self) -> bool:
            if not password:
                return True
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                _, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except Exception:
                return False
            return hmac.compare_digest(pw.encode(), password.encode())

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            if status == 401:
                self.send_header("WWW-Authenticate", 'Basic realm="ticket-watch", charset="UTF-8"')
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, status: int = 200) -> None:
            self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _body(self) -> dict:
            # 只接受 JSON：跨站表單無法送 application/json，可擋 CSRF
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                raise ApiError("Content-Type 必須是 application/json", 415)
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                raise ApiError("來源不允許", 403)
            length = int(self.headers.get("Content-Length") or 0)
            if length > 100_000:
                raise ApiError("資料太大", 413)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except ValueError:
                raise ApiError("JSON 格式錯誤")
            if not isinstance(data, dict):
                raise ApiError("JSON 格式錯誤")
            return data

        def _dispatch(self, method: str) -> None:
            if not self._authorized():
                self._send(401, "需要密碼".encode(), "text/plain; charset=utf-8")
                return
            url = urlparse(self.path)
            q = {k: v[-1] for k, v in parse_qs(url.query).items()}
            try:
                if method == "GET" and url.path in ("/", "/index.html"):
                    with open(os.path.join(STATIC, "index.html"), "rb") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
                    return
                result = route(svc, method, url.path, q, self._body if method == "POST" else dict)
                self._json(result)
            except ApiError as e:
                self._json({"error": str(e)}, e.status)
            except ValueError as e:
                self._json({"error": str(e)}, 400)
            except Exception:
                log.exception("API 錯誤 %s %s", method, self.path)
                self._json({"error": "伺服器錯誤，請看 log"}, 500)

        def do_GET(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

    return Handler


def route(svc: Service, method: str, path: str, q: dict, body):
    """API 路由。body() 會讀取並驗證 POST 的 JSON。"""
    if method == "GET":
        if path == "/api/overview":
            return svc.overview()
        if path == "/api/calendar":
            return svc.calendar(q.get("route", "TPE-KIX"))
        if path == "/api/date":
            return svc.date_detail(q.get("route", "TPE-KIX"), q.get("date", ""))
        if path == "/api/roundtrip":
            return svc.roundtrip(
                q.get("outbound", "TPE-KIX"),
                q["from"],
                q["to"],
                int(q.get("min", 3)),
                int(q.get("max", 5)),
                int(q["max_total"]) if q.get("max_total") else None,
            )
        if path == "/api/jobs":
            return {"jobs": svc.list_jobs()}
        m = re.fullmatch(r"/api/jobs/([0-9a-f]+)", path)
        if m:
            job = svc.job_dict(m[1])
            if job is None:
                raise ApiError("找不到這個查詢", 404)
            return job
        if path == "/api/rules":
            return {"rules": svc.settings.to_dict()["rules"]}
        if path == "/api/system":
            return svc.system()
    elif method == "POST":
        data = body()
        if path == "/api/search":
            job = svc.request_search(data.get("legs") or [], data.get("max_age_hours", 3))
            return svc.job_dict(job.id)
        if path == "/api/scan":
            return svc.job_dict(svc.request_scan().id)
        m = re.fullmatch(r"/api/jobs/([0-9a-f]+)/cancel", path)
        if m:
            return {"ok": svc.cancel_job(m[1])}
        if path == "/api/rules":
            rule = svc.settings.upsert_rule(data)
            return {"rule": asdict(rule)}
        m = re.fullmatch(r"/api/rules/([0-9a-f]+)/delete", path)
        if m:
            return {"ok": svc.settings.delete_rule(m[1])}
        if path == "/api/system":
            svc.settings.update_system(data)
            return svc.system()
        if path == "/api/telegram/test":
            ok = svc.notifier.send("👋 機票監控測試訊息：Telegram 通知運作正常")
            if not ok:
                raise ApiError("發送失敗，請檢查 token / chat id（詳見 log）", 502)
            return {"ok": True}
    raise ApiError("找不到這個 API", 404)


def serve(svc: Service, host: str, port: int, password: str) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(svc, password))
    httpd.daemon_threads = True
    return httpd
