import os
import stat
import subprocess
import threading
import time

import pytest

from ticket_watch.tunnel import Tunnel, find_binary
from ticket_watch.web import AuthGuard

FAKE = """#!/bin/sh
# 假的 cloudflared：第 1 次印出網址後結束（模擬斷線），第 2 次印出新網址後常駐
n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "{counter}"
echo "args: $*"
echo "2026-10-11T00:00:00Z INF Requesting new quick Tunnel on trycloudflare.com..."
echo "2026-10-11T00:00:01Z INF |  https://run-$n-abc.trycloudflare.com                     |"
if [ "$n" = 1 ]; then exit 1; fi
exec sleep 60
"""


@pytest.fixture
def fake_bin(tmp_path):
    path = tmp_path / "cloudflared"
    path.write_text(FAKE.format(counter=tmp_path / "count"))
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def wait_for(cond, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_quick_tunnel_restarts_and_reports_new_urls(tmp_path, fake_bin):
    urls = []
    t = Tunnel(fake_bin, 8787, str(tmp_path / "state"), urls.append, health_interval=3600, sleep=lambda s: None)
    t.start()
    try:
        assert wait_for(lambda: len(urls) == 2)
        assert urls == ["https://run-1-abc.trycloudflare.com", "https://run-2-abc.trycloudflare.com"]
        assert wait_for(lambda: t.connected) and t.url == urls[-1] and t.restarts == 1
        assert open(tmp_path / "state" / "public_url").read() == urls[-1]
        assert "--url http://127.0.0.1:8787" in " ".join(t.recent)
        pid = int(open(tmp_path / "state" / "cloudflared.pid").read())
    finally:
        t.stop()
    assert wait_for(lambda: subprocess.run(["ps", "-p", str(pid)], capture_output=True).returncode != 0)
    assert not os.path.exists(tmp_path / "state" / "cloudflared.pid")


def test_same_url_not_reported_twice(tmp_path):
    urls = []
    t = Tunnel("x", 1, str(tmp_path), urls.append)
    t.handle_line("INF |  https://same-url.trycloudflare.com  |")
    t.handle_line("INF |  https://same-url.trycloudflare.com  |")
    t.handle_line("INF Requesting new quick Tunnel on https://api.trycloudflare.com")
    assert urls == ["https://same-url.trycloudflare.com"]
    # 服務重啟後（同一個網址檔），網址沒變也不重複通知
    t2 = Tunnel("x", 1, str(tmp_path), urls.append)
    t2.handle_line("https://same-url.trycloudflare.com")
    assert len(urls) == 1


def test_token_mode_uses_fixed_url(tmp_path):
    urls = []
    t = Tunnel("cf", 8787, str(tmp_path), urls.append, token="TKN", public_url="https://fare.example.com/")
    assert t.command() == ["cf", "tunnel", "--no-autoupdate", "run", "--token", "TKN"]
    assert t.mode == "token" and t.url == "https://fare.example.com"
    t.handle_line("INF Starting tunnel")
    assert urls == [] and not t.connected
    t.handle_line("INF Registered tunnel connection connIndex=0")
    assert urls == ["https://fare.example.com"] and t.connected


def test_kill_orphan(tmp_path):
    os.makedirs(tmp_path / "bin")
    orphan = tmp_path / "bin" / "cloudflared"
    orphan.write_text("#!/bin/sh\nsleep 60\n")
    orphan.chmod(0o755)
    p = subprocess.Popen([str(orphan)])
    (tmp_path / "cloudflared.pid").write_text(str(p.pid))
    Tunnel("x", 1, str(tmp_path), lambda u: None).kill_orphan()
    assert p.wait(timeout=5) != 0
    # 不是 cloudflared 的 pid 不會被誤殺
    other = subprocess.Popen(["sleep", "60"])
    (tmp_path / "cloudflared.pid").write_text(str(other.pid))
    Tunnel("x", 1, str(tmp_path), lambda u: None).kill_orphan()
    assert other.poll() is None
    other.kill()


def test_find_binary_prefers_local(tmp_path, fake_bin):
    os.makedirs(tmp_path / "inst" / "bin")
    local = tmp_path / "inst" / "bin" / "cloudflared"
    local.write_text("#!/bin/sh\n")
    local.chmod(0o755)
    assert find_binary(str(tmp_path / "inst")) == str(local)


def test_auth_guard_blocks_per_ip_and_expires():
    now = {"t": 0.0}
    g = AuthGuard(clock=lambda: now["t"])
    for _ in range(9):
        g.failed("1.1.1.1")
    assert not g.blocked("1.1.1.1")
    g.failed("1.1.1.1")
    assert g.blocked("1.1.1.1") and not g.blocked("2.2.2.2")
    now["t"] += AuthGuard.WINDOW + 1
    assert not g.blocked("1.1.1.1")


def test_auth_guard_global_limit():
    g = AuthGuard(clock=lambda: 0.0)
    for i in range(AuthGuard.GLOBAL):
        g.failed(f"10.0.{i // 250}.{i % 250}")
    assert g.blocked("9.9.9.9")
