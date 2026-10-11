"""Cloudflare Tunnel：讓家裡的網站有一個 https 公網網址。

兩種模式：
- quick（預設）：`cloudflared tunnel --url ...`，不用帳號，網址是隨機的 *.trycloudflare.com，
  每次 cloudflared 重啟網址都會變 → 變了就發 Telegram 告訴你新網址
- token：在 Cloudflare 後台建立 Tunnel 並綁自己的網域，網址固定（設定 CF_TUNNEL_TOKEN + PUBLIC_URL）

cloudflared 由這裡當子程序管理：掛了自動重啟（指數退避），
並定期從外面打 /healthz，連續失敗就重啟（快速通道偶爾會默默斷線）。
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from typing import Callable

import requests

log = logging.getLogger("ticket_watch.tunnel")

QUICK_URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
HEALTH_INTERVAL = 600  # 秒
HEALTH_FAILS_BEFORE_RESTART = 3


def find_binary(install_dir: str) -> str:
    local = os.path.join(install_dir, "bin", "cloudflared")
    if os.path.isfile(local) and os.access(local, os.X_OK):
        return local
    return shutil.which("cloudflared") or ""


class Tunnel:
    def __init__(
        self,
        binary: str,
        port: int,
        state_dir: str,
        on_url: Callable[[str], None],
        token: str = "",
        public_url: str = "",
        health_interval: float = HEALTH_INTERVAL,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.binary = binary
        self.port = port
        self.state_dir = state_dir
        self.on_url = on_url
        self.token = token
        self.fixed_url = public_url.rstrip("/")
        self.health_interval = health_interval
        self.sleep = sleep
        self.url = self.fixed_url
        self.connected = False
        self.proc: subprocess.Popen | None = None
        self.stop_event = threading.Event()
        self.recent: deque[str] = deque(maxlen=20)
        self.restarts = 0

    @property
    def mode(self) -> str:
        return "token" if self.token else "quick"

    @property
    def pid_file(self) -> str:
        return os.path.join(self.state_dir, "cloudflared.pid")

    @property
    def url_file(self) -> str:
        return os.path.join(self.state_dir, "public_url")

    def command(self) -> list[str]:
        base = [self.binary, "tunnel", "--no-autoupdate"]
        if self.token:
            return base + ["run", "--token", self.token]
        return base + ["--url", f"http://127.0.0.1:{self.port}"]

    # ---------------- 子程序管理

    def kill_orphan(self) -> None:
        """上一個服務被強制結束時留下的 cloudflared，先清掉。"""
        try:
            pid = int(open(self.pid_file).read().strip())
        except (OSError, ValueError):
            return
        try:
            args = subprocess.run(["ps", "-p", str(pid), "-o", "args="], capture_output=True, text=True, timeout=5).stdout
        except Exception:
            return
        if "cloudflared" in args:
            log.info("清除殘留的 cloudflared（pid %s）", pid)
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass

    def _spawn(self) -> subprocess.Popen:
        proc = subprocess.Popen(
            self.command(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        with open(self.pid_file, "w") as f:
            f.write(str(proc.pid))
        return proc

    def handle_line(self, line: str) -> None:
        self.recent.append(line.rstrip())
        if self.token:
            if "Registered tunnel connection" in line and not self.connected:
                self.connected = True
                self._set_url(self.fixed_url)
            return
        m = QUICK_URL_RE.search(line)
        if m and "api.trycloudflare.com" not in m.group(0):
            self.connected = True
            self._set_url(m.group(0))

    def _set_url(self, url: str) -> None:
        if not url:
            return
        self.url = url
        try:
            previous = open(self.url_file).read().strip()
        except OSError:
            previous = ""
        with open(self.url_file, "w") as f:
            f.write(url)
        if url != previous:
            log.info("公網網址：%s", url)
            try:
                self.on_url(url)
            except Exception:
                log.exception("通知新網址失敗")

    def run_once(self) -> int:
        """啟動一次 cloudflared 並讀輸出直到結束，回傳 exit code。"""
        self.connected = False
        if not self.token:
            self.url = ""
        self.proc = self._spawn()
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.handle_line(line)
        return self.proc.wait()

    def supervise(self) -> None:
        backoff = 5.0
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                code = self.run_once()
            except OSError as e:
                log.error("無法啟動 cloudflared：%s", e)
                code = -1
            if self.stop_event.is_set():
                break
            self.connected = False
            self.restarts += 1
            if time.monotonic() - started > 300:
                backoff = 5.0  # 跑了一陣子才掛，視為偶發
            log.warning(
                "cloudflared 結束（code %s），%d 秒後重啟。最後輸出：\n%s",
                code,
                backoff,
                "\n".join(list(self.recent)[-5:]),
            )
            self.sleep(backoff)
            backoff = min(backoff * 2, 300)

    def health_loop(self) -> None:
        fails = 0
        while not self.stop_event.wait(self.health_interval):
            if not self.url or not self.connected:
                continue
            try:
                ok = requests.get(self.url + "/healthz", timeout=20).text.strip() == "ok"
            except Exception:
                ok = False
            fails = 0 if ok else fails + 1
            if fails >= HEALTH_FAILS_BEFORE_RESTART:
                log.warning("公網網址連續 %d 次連不上，重啟 cloudflared", fails)
                fails = 0
                self.restart()

    def restart(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def start(self) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        self.kill_orphan()
        threading.Thread(target=self.supervise, daemon=True, name="tunnel").start()
        threading.Thread(target=self.health_loop, daemon=True, name="tunnel-health").start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        try:
            os.remove(self.pid_file)
        except OSError:
            pass

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "url": self.url,
            "connected": self.connected,
            "restarts": self.restarts,
        }
