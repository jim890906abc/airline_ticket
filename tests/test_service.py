import base64
import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from ticket_watch.config import Config
from ticket_watch.google_flights import BlockedError
from ticket_watch.models import Fare
from ticket_watch.service import Gate, Service
from ticket_watch.web import serve

NOW = datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc)


class FakeTG:
    def __init__(self):
        self.sent = []

    def send(self, text, reply_markup=None):
        self.sent.append(text)
        return True


def fare(o, dst, d, price, dep="07:00"):
    return Fare(d, price, "台灣虎航", dep, "10:30", 0, "google", f"https://www.google.com/travel/flights?d={d}", o, dst)


def default_search(o, dst, d):
    day = int(d[-2:])
    return [fare(o, dst, d, 3000 + day * 50 + (0 if o == "TPE" else 200)), fare(o, dst, d, 9000, "18:00")]


def make_svc(tmp_path, search=default_search, now=NOW, **kw):
    c = Config(state_file=str(tmp_path / "state.json"), summary_hour=-1, **kw)
    calls = []

    def counted(o, dst, d):
        calls.append((o, dst, d))
        return search(o, dst, d)

    svc = Service(c, FakeTG(), counted, now_fn=lambda: now, gate_sleep=lambda _: None, install_dir="/home/u/airline_ticket")
    svc.calls = calls
    return svc


def drain(svc):
    while not svc.queue.empty():
        svc.process(svc.jobs[svc.queue.get()])


# ---------------------------------------------------------------- service


def test_search_job_records_and_reuses_cache(tmp_path):
    svc = make_svc(tmp_path)
    legs = [{"origin": "TPE", "destination": "KIX", "date_from": "2026-11-01", "date_to": "2026-11-05"}]
    job = svc.request_search(legs, max_age_hours=3)
    assert job.total == 5 and job.to_fetch == 5
    drain(svc)
    d = svc.job_dict(job.id)
    assert d["status"] == "done" and d["done"] == 5 and len(svc.calls) == 5
    assert svc.state.info("TPE-KIX", "2026-11-03")["min_price"] == 3150

    job2 = svc.request_search(legs + [{**legs[0], "origin": "KIX", "destination": "TPE"}], max_age_hours=3)
    assert job2.total == 10 and job2.to_fetch == 5  # 去程沿用快取
    drain(svc)
    assert len(svc.calls) == 10
    assert sum(r["from_cache"] for r in svc.job_dict(job2.id)["results"]) == 5


def test_search_validation(tmp_path):
    svc = make_svc(tmp_path)
    leg = {"origin": "TPE", "destination": "KIX"}
    with pytest.raises(ValueError, match="最多"):
        svc.request_search([{**leg, "date_from": "2026-11-01", "date_to": "2027-03-01"}])
    with pytest.raises(ValueError, match="航線"):
        svc.request_search([{"origin": "TPE", "destination": "HND", "date_from": "2026-11-01"}])
    with pytest.raises(ValueError, match="晚於"):
        svc.request_search([{**leg, "date_from": "2026-11-05", "date_to": "2026-11-01"}])
    with pytest.raises(ValueError, match="330"):
        svc.request_search([{**leg, "date_from": "2028-01-01"}])
    job = svc.request_search([{**leg, "date_from": "2026-11-01", "date_to": "2026-11-30", "weekdays": [5, 6]}])
    assert job.total == 9  # 11 月的週六日


def test_search_alerts_on_matching_rule_and_respects_pause(tmp_path):
    svc = make_svc(tmp_path)  # 預設規則：TPE→KIX 未來 120 天 < 3500
    leg = {"origin": "TPE", "destination": "KIX", "date_from": "2026-11-01", "date_to": "2026-11-02"}
    svc.settings.update_system({"paused": True})
    svc.request_search([leg], 0)
    drain(svc)
    assert svc.notifier.sent == [] and svc.state.notified == {}
    svc.settings.update_system({"paused": False})
    svc.request_search([leg], 0)
    drain(svc)
    assert len(svc.notifier.sent) == 1 and "NT$3,050" in svc.notifier.sent[0]


def test_search_stops_when_blocked(tmp_path):
    def search(o, dst, d):
        raise BlockedError("HTTP 429")

    svc = make_svc(tmp_path, search)
    job = svc.request_search([{"origin": "TPE", "destination": "KIX", "date_from": "2026-11-01", "date_to": "2026-11-10"}], 0)
    drain(svc)
    d = svc.job_dict(job.id)
    assert len(svc.calls) == 1 and "限流" in d["message"] and d["results"][0]["error"]


def test_cancel_queued_and_running(tmp_path):
    svc = make_svc(tmp_path)
    leg = {"origin": "TPE", "destination": "KIX", "date_from": "2026-11-01", "date_to": "2026-11-10"}
    j1 = svc.request_search([leg], 0)
    assert svc.cancel_job(j1.id) and svc.jobs[j1.id].status == "cancelled"
    drain(svc)
    assert svc.calls == []

    def search(o, dst, d):
        if len(svc2.calls) == 3:  # 查到第 3 個時使用者按取消
            svc2.cancel_job(j2.id)
        return default_search(o, dst, d)

    svc2 = make_svc(tmp_path, search)
    j2 = svc2.request_search([leg], 0)
    drain(svc2)
    assert svc2.jobs[j2.id].status == "cancelled" and len(svc2.calls) == 3


def test_scan_job_uses_rules_and_cooldown(tmp_path):
    svc = make_svc(tmp_path)
    svc.settings.update_system({"dates_per_run": 4})
    job = svc.request_scan()
    drain(svc)
    d = svc.job_dict(job.id)
    assert d["status"] == "done" and d["total"] == 4 and len(svc.calls) == 4
    assert all(c[:2] == ("TPE", "KIX") for c in svc.calls)
    with pytest.raises(ValueError, match="分鐘後"):
        svc.request_scan()
    assert timedelta(hours=2, minutes=40) <= svc.next_scan - NOW <= timedelta(hours=3, minutes=20)


def test_scheduled_scan_resumes_after_restart(tmp_path):
    svc = make_svc(tmp_path)
    svc.request_scan()
    drain(svc)
    later = make_svc(tmp_path, now=NOW + timedelta(hours=1))
    assert later.next_scan == NOW + timedelta(hours=3)
    assert len(later.state.dates) == 12


def test_roundtrip_combos(tmp_path):
    svc = make_svc(tmp_path)
    svc.request_search(
        [
            {"origin": "TPE", "destination": "KIX", "date_from": "2026-11-01", "date_to": "2026-11-03"},
            {"origin": "KIX", "destination": "TPE", "date_from": "2026-11-04", "date_to": "2026-11-07"},
        ],
        0,
    )
    drain(svc)
    rt = svc.roundtrip("TPE-KIX", "2026-11-01", "2026-11-03", 3, 4)
    best = rt["combos"][0]
    # 去程 11/01 = 3050，回程 11/04 = 3400
    assert best["total"] == 6450 and best["outbound"]["date"] == "2026-11-01" and best["inbound"]["date"] == "2026-11-04"
    assert len(rt["combos"]) == 6 and rt["missing_outbound"] == 0 and rt["missing_inbound"] == 0
    assert rt["inbound_range"] == ["2026-11-04", "2026-11-07"]
    assert svc.roundtrip("TPE-KIX", "2026-11-01", "2026-11-03", 3, 4, max_total=6500)["combos"][-1]["total"] <= 6500
    assert svc.roundtrip("TPE-KIX", "2026-11-01", "2026-11-05", 3, 3)["missing_outbound"] == 2


def test_calendar_marks_rule_coverage(tmp_path):
    svc = make_svc(tmp_path)
    svc.request_search([{"origin": "TPE", "destination": "KIX", "date_from": "2026-11-01", "date_to": "2026-11-01"}], 0)
    drain(svc)
    cal = svc.calendar("TPE-KIX")["dates"]
    assert cal["2026-11-01"]["min_price"] == 3050 and cal["2026-11-01"]["threshold"] == 3500
    assert cal["2026-10-20"] == {"min_price": None, "airline": "", "checked_at": "", "threshold": 3500}
    assert "2026-10-20" not in svc.calendar("KIX-TPE")["dates"]  # 回程沒有規則


def test_gate_spaces_requests():
    t = {"now": 0.0}
    slept = []

    def sleep(s):
        slept.append(s)
        t["now"] += s

    g = Gate(lambda *a: [], Config(delay_min=10, delay_max=10), sleep=sleep, clock=lambda: t["now"])
    g.search("TPE", "KIX", "a")
    t["now"] += 3
    g.search("TPE", "KIX", "b")
    assert slept == [pytest.approx(7)]


# ---------------------------------------------------------------- web


@pytest.fixture
def server(tmp_path):
    svc = make_svc(tmp_path)
    httpd = serve(svc, "127.0.0.1", 0, "pw123")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield svc, base
    httpd.shutdown()


def call(base, path, body=None, auth="pw123", ctype="application/json", headers=None):
    h = {"Content-Type": ctype, **(headers or {})}
    if auth is not None:
        h["Authorization"] = "Basic " + base64.b64encode(f"admin:{auth}".encode()).decode()
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, headers=h, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw.decode())
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode()


def test_web_requires_password(server):
    svc, base = server
    assert call(base, "/api/overview", auth=None)[0] == 401
    assert call(base, "/api/overview", auth="wrong")[0] == 401
    status, page = call(base, "/")
    assert status == 200 and "桃園 ⇄ 大阪" in page


def test_web_csrf_guards(server):
    svc, base = server
    assert call(base, "/api/scan", {}, ctype="text/plain")[0] == 415
    assert call(base, "/api/scan", {}, headers={"Origin": "http://evil.example"})[0] == 403


def test_web_end_to_end(server):
    svc, base = server
    status, ov = call(base, "/api/overview")
    assert status == 200 and ov["watch"]["targets"] == 120 and ov["cheapest"]["TPE-KIX"] == []

    # 新增回程規則
    status, r = call(base, "/api/rules", {"name": "回程", "origin": "KIX", "destination": "TPE", "date_from": "2026-11-01", "date_to": "2026-11-10", "max_price": 3300})
    assert status == 200 and r["rule"]["id"]
    assert len(call(base, "/api/rules")[1]["rules"]) == 2
    assert call(base, "/api/rules", {"origin": "KIX", "destination": "TPE", "max_price": 1})[0] == 400

    # 即時查詢 → 背景處理 → 取得結果
    status, job = call(base, "/api/search", {"legs": [{"origin": "KIX", "destination": "TPE", "date_from": "2026-11-01", "date_to": "2026-11-02"}], "max_age_hours": 0})
    assert status == 200 and job["status"] == "queued" and job["total"] == 2
    drain(svc)
    status, job = call(base, f"/api/jobs/{job['id']}")
    assert job["status"] == "done" and job["results"][0]["fares"][0]["price"] in (3250, 3300)
    assert any("大阪關西 → 桃園" in t for t in svc.notifier.sent)  # 3250 < 3300 → 推播

    status, ov = call(base, "/api/overview")
    assert ov["cheapest"]["KIX-TPE"][0]["price"] == 3250
    assert call(base, "/api/date?route=KIX-TPE&date=2026-11-01")[1]["fares"][0]["price"] == 3250
    assert "2026-11-01" in call(base, "/api/calendar?route=KIX-TPE")[1]["dates"]
    assert call(base, "/api/roundtrip?outbound=TPE-KIX&from=2026-11-01&to=2026-11-02&min=1&max=3")[0] == 200

    # 系統設定
    status, sysinfo = call(base, "/api/system", {"dates_per_run": 8, "paused": True})
    assert sysinfo["settings"] == {"paused": True, "dates_per_run": 8, "scan_interval_hours": 3.0}
    assert "install.sh | TELEGRAM_BOT_TOKEN=" in sysinfo["install_cmd"]
    assert sysinfo["update_cmd"].startswith("cd /home/u/airline_ticket && git fetch")
    assert call(base, "/api/system", {"dates_per_run": 999})[0] == 400

    # 刪除規則、Telegram 測試、不存在的 API
    rid = r["rule"]["id"]
    assert call(base, f"/api/rules/{rid}/delete", {})[1]["ok"]
    assert call(base, "/api/telegram/test", {})[1]["ok"]
    assert call(base, "/api/nope")[0] == 404
    assert call(base, "/api/jobs/deadbeef")[0] == 404
