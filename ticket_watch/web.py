"""網站：標準函式庫的 HTTP server，提供單頁介面與 JSON API。"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import deque
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .service import Service

log = logging.getLogger("ticket_watch.web")

STATIC = os.path.join(os.path.dirname(__file__), "static")


class AuthGuard:
    """擋暴力猜密碼：同一個 IP 15 分鐘內錯 10 次就先封鎖；全部來源加總錯太多也會暫停。"""

    WINDOW = 15 * 60
    PER_IP = 10
    GLOBAL = 100

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.fails: dict[str, deque] = {}
        self.all_fails: deque = deque()

    def _trim(self, q: deque, now: float) -> None:
        while q and now - q[0] > self.WINDOW:
            q.popleft()

    def blocked(self, ip: str) -> bool:
        now = self.clock()
        with self.lock:
            q = self.fails.get(ip)
            if q is not None:
                self._trim(q, now)
            self._trim(self.all_fails, now)
            return (q is not None and len(q) >= self.PER_IP) or len(self.all_fails) >= self.GLOBAL

    def failed(self, ip: str) -> None:
        now = self.clock()
        with self.lock:
            self.fails.setdefault(ip, deque()).append(now)
            self.all_fails.append(now)
            if len(self.fails) > 10000:  # 避免被灌爆記憶體
                self.fails.clear()

    def succeeded(self, ip: str) -> None:
        with self.lock:
            self.fails.pop(ip, None)


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def make_handler(svc: Service, password: str, guard: AuthGuard | None = None):
    guard = guard or AuthGuard()

    class Handler(BaseHTTPRequestHandler):
        server_version = "TicketWatch"

        def log_message(self, fmt, *args):  # 安靜一點，只記錯誤
            pass

        # ---------------- 基本工具

        def _client_ip(self) -> str:
            ip = self.client_address[0]
            # 經由 Cloudflare Tunnel 進來的請求都來自本機，真正的 IP 在 CF-Connecting-IP
            if ip in ("127.0.0.1", "::1") and self.headers.get("CF-Connecting-IP"):
                return self.headers["CF-Connecting-IP"].strip()
            return ip

        def _check_password(self) -> bool:
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                _, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except Exception:
                return False
            return hmac.compare_digest(pw.encode(), password.encode())

        def _authorized(self) -> str:
            """回傳 "ok" / "missing" / "wrong" / "blocked"。"""
            if not password:
                return "ok"
            ip = self._client_ip()
            if guard.blocked(ip):
                return "blocked"
            if not self.headers.get("Authorization"):
                return "missing"  # 瀏覽器第一次請求不帶密碼，不算猜錯
            if self._check_password():
                guard.succeeded(ip)
                return "ok"
            guard.failed(ip)
            log.warning("密碼錯誤：%s", ip)
            return "wrong"

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
            allowed = {self.headers.get("Host"), self.headers.get("X-Forwarded-Host")}
            if svc.public_url():
                allowed.add(urlparse(svc.public_url()).netloc)
            if origin and urlparse(origin).netloc not in allowed:
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
            url = urlparse(self.path)
            if url.path == "/healthz":  # 給 tunnel 健康檢查，不需要密碼、不透露任何資料
                self._send(200, b"ok", "text/plain")
                return
            auth = self._authorized()
            if auth == "blocked":
                self._send(429, "密碼錯誤太多次，請 15 分鐘後再試".encode(), "text/plain; charset=utf-8")
                return
            if auth != "ok":
                self._send(401, "需要密碼".encode(), "text/plain; charset=utf-8")
                return
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


def serve(svc: Service, host: str, port: int, password: str, guard: AuthGuard | None = None) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), make_handler(svc, password, guard))
    httpd.daemon_threads = True
    return httpd
