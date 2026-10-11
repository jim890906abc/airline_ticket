from datetime import date, datetime, timedelta, timezone

import pytest

from ticket_watch import bot as botmod
from ticket_watch.bot import (
    BTN_CHEAPEST,
    BTN_DATE,
    BTN_MONTHS,
    BTN_PAUSE,
    BTN_RESUME,
    BTN_SCAN,
    BTN_STATUS,
    Bot,
    Gate,
    parse_date,
)
from ticket_watch.config import Config
from ticket_watch.google_flights import BlockedError
from ticket_watch.models import Fare

NOW = datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc)  # 台北 10:00
CHAT = "42"


class FakeTG:
    def __init__(self):
        self.chat_id = CHAT
        self.sent = []  # (text, markup)
        self.calls = []

    def send(self, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return True

    def call(self, method, payload=None, timeout=30):
        self.calls.append((method, payload))
        return True

    @property
    def last(self):
        return self.sent[-1][0]


def fare(d, price, dep="07:00", airline="台灣虎航"):
    return Fare(d, price, airline, dep, "10:30", 0, "google", f"https://www.google.com/travel/flights?d={d}&x=1")


def make_bot(tmp_path, search=None, now=NOW, **cfg_kw):
    kw = dict(summary_hour=-1, dates_per_run=3, days_ahead_max=30)
    kw.update(cfg_kw)
    cfg = Config(state_file=str(tmp_path / "state.json"), **kw)
    tg = FakeTG()
    b = Bot(cfg, tg, search or (lambda d: []), now_fn=lambda: now, gate_sleep=lambda _: None)
    return b, tg


def msg(text, chat=CHAT, uid=1):
    return {"update_id": uid, "message": {"chat": {"id": int(chat)}, "text": text}}


def cb(data, chat=CHAT):
    return {"update_id": 1, "callback_query": {"id": "q", "data": data, "message": {"chat": {"id": int(chat)}}}}


def drain(b):
    while not b.jobs.empty():
        b.process_job(b.jobs.get())


# ---------------------------------------------------------------- 基本


def test_parse_date():
    today = date(2026, 10, 11)
    assert parse_date("2026-11-18", today) == date(2026, 11, 18)
    assert parse_date("2026/11/18", today) == date(2026, 11, 18)
    assert parse_date("11/18", today) == date(2026, 11, 18)
    assert parse_date("3/5", today) == date(2027, 3, 5)  # 已過 → 明年
    assert parse_date("2/30", today) is None
    assert parse_date("hello", today) is None


def test_ignores_other_chats(tmp_path):
    b, tg = make_bot(tmp_path)
    b.handle_update(msg("/start", chat="999"))
    b.handle_update(cb("thr:3000", chat="999"))
    assert tg.sent == []
    assert b.settings.max_price == 3500


def test_menu_has_buttons(tmp_path):
    b, tg = make_bot(tmp_path)
    b.handle_update(msg("/start"))
    kb = tg.sent[-1][1]["keyboard"]
    labels = [x for row in kb for x in row]
    assert BTN_CHEAPEST in labels and BTN_PAUSE in labels and BTN_STATUS in labels


# ---------------------------------------------------------------- 最便宜 5 張


def test_cheapest_five_after_scan(tmp_path):
    prices = iter([3300, 4100, 3900, 5000, 2999, 3700])

    def search(d):
        return [fare(d, next(prices)), fare(d, 9000, dep="15:00")]

    b, tg = make_bot(tmp_path, search, dates_per_run=6)
    b.handle_update(msg(BTN_SCAN))
    drain(b)
    assert "掃描完成：查了 6 個日期" in tg.last

    b.handle_update(msg(BTN_CHEAPEST))
    text, markup = tg.sent[-1]
    order = [int(x.replace(",", "")) for x in __import__("re").findall(r"NT\$([\d,]+)", text)]
    assert order == [2999, 3300, 3700, 3900, 4100]
    assert text.count("🔗 https://www.google.com/travel/flights") == 5
    assert "&amp;x=1" in text  # URL 有正確跳脫
    assert "剛剛查" in text
    assert markup["inline_keyboard"][0][0]["callback_data"] == "recheck"


def test_cheapest_without_data(tmp_path):
    b, tg = make_bot(tmp_path)
    b.handle_update(msg("/cheapest"))
    assert "還沒有價格資料" in tg.last


def test_recheck_updates_prices(tmp_path):
    price = {"v": 3300}
    b, tg = make_bot(tmp_path, lambda d: [fare(d, price["v"])], dates_per_run=1)
    b.handle_update(msg(BTN_SCAN))
    drain(b)
    price["v"] = 3100
    b.handle_update(cb("recheck"))
    drain(b)
    assert "NT$3,100" in tg.last and "NT$3,300" not in tg.last


# ---------------------------------------------------------------- 查日期


def test_date_flow(tmp_path):
    queried = []

    def search(d):
        queried.append(d)
        return [fare(d, 3200), fare(d, 4800, dep="18:00")]

    b, tg = make_bot(tmp_path, search)
    b.handle_update(msg(BTN_DATE))
    assert "請輸入出發日期" in tg.last
    b.handle_update(msg("11/18"))
    drain(b)
    assert queried == ["2026-11-18"]
    assert "2026-11-18（三）" in tg.last and "NT$3,200</b>🔥" in tg.last and "NT$4,800" in tg.last
    # 已顯示給使用者 → 標記為已通知，下一輪不重複推播
    assert "2026-11-18|台灣虎航|07:00" in b.state.notified
    assert b.state.dates["2026-11-18"]["min_price"] == 3200


def test_date_bare_text_and_validation(tmp_path):
    b, tg = make_bot(tmp_path, lambda d: [])
    b.handle_update(msg("/date 2028-01-01"))
    assert "330 天內" in tg.last
    b.handle_update(msg("2026-11-18"))
    drain(b)
    assert "沒有找到直飛航班" in tg.last


def test_date_query_blocked(tmp_path):
    def search(d):
        raise BlockedError("HTTP 429")

    b, tg = make_bot(tmp_path, search)
    b.handle_update(msg("/date 11/18"))
    drain(b)
    assert "查詢失敗" in tg.last


# ---------------------------------------------------------------- 門檻 / 暫停


def test_threshold_presets_and_custom(tmp_path):
    b, tg = make_bot(tmp_path)
    b.handle_update(msg("💰 設定門檻"))
    assert "thr:3000" in str(tg.sent[-1][1])
    b.handle_update(cb("thr:4000"))
    assert b.settings.max_price == 4000 and "4,000" in tg.last
    b.handle_update(cb("thr:custom"))
    b.handle_update(msg("3,800"))
    assert b.settings.max_price == 3800
    b.handle_update(msg("/threshold 50"))
    assert "格式不對" in tg.last and b.settings.max_price == 3800

    b2, _ = make_bot(tmp_path)  # 重開後設定仍在
    assert b2.settings.max_price == 3800


def test_threshold_affects_scan_alerts(tmp_path):
    b, tg = make_bot(tmp_path, lambda d: [fare(d, 3800)], dates_per_run=1)
    b.handle_update(cb("thr:4000"))
    b.process_job(("scan", False))
    assert any("便宜機票" in t for t, _ in tg.sent)


def test_pause_mutes_alerts_then_resume_delivers(tmp_path):
    b, tg = make_bot(tmp_path, lambda d: [fare(d, 3000)], dates_per_run=1)
    b.handle_update(msg(BTN_PAUSE))
    assert b.settings.paused
    assert BTN_RESUME in str(tg.sent[-1][1])  # 按鈕變成「恢復」
    b.process_job(("scan", False))
    assert not any("便宜機票" in t for t, _ in tg.sent)
    assert b.state.notified == {}

    b.handle_update(msg(BTN_RESUME))
    d = next(iter(b.state.dates))
    b.state.dates[d]["checked_at"] = (NOW - timedelta(days=9)).isoformat()  # 讓同一天再被挑到
    b.process_job(("scan", False))
    assert any("便宜機票" in t for t, _ in tg.sent)


# ---------------------------------------------------------------- 掃描 / 狀態


def test_manual_scan_cooldown(tmp_path):
    b, tg = make_bot(tmp_path)
    b.handle_update(msg(BTN_SCAN))
    drain(b)
    b.handle_update(msg(BTN_SCAN))
    assert "分鐘後再試" in tg.last and b.jobs.empty()


def test_scan_persists_and_reschedules(tmp_path):
    b, tg = make_bot(tmp_path, lambda d: [fare(d, 4000)])
    b.process_job(("scan", False))
    assert b.state.last_scan_at == NOW.isoformat()
    assert timedelta(hours=2, minutes=40) <= b.next_scan - NOW <= timedelta(hours=3, minutes=20)
    b2, _ = make_bot(tmp_path, now=NOW + timedelta(hours=1))
    assert len(b2.state.dates) == 3
    assert b2.next_scan == NOW + timedelta(hours=3)  # 重啟後沿用排程，不會馬上又掃


def test_queue_message_when_busy(tmp_path):
    b, tg = make_bot(tmp_path)
    b.busy = "掃描"
    b.handle_update(msg("/date 11/18"))
    assert "已排入佇列" in tg.last


def test_months_and_status(tmp_path):
    b, tg = make_bot(tmp_path, lambda d: [fare(d, 3400 if d.endswith("-20") else 4500)], dates_per_run=30)
    b.process_job(("scan", False))
    b.handle_update(msg(BTN_MONTHS))
    assert "<b>2026-10</b>  NT$3,400🔥" in tg.last
    assert "<b>2026-11</b>  NT$4,500" in tg.last
    b.handle_update(msg(BTN_STATUS))
    assert "已掌握 30/30 天" in tg.last and "通知門檻：NT$3,500" in tg.last


def test_unknown_text(tmp_path):
    b, tg = make_bot(tmp_path)
    b.handle_update(msg("你好"))
    assert "看不懂" in tg.last


def test_gate_spaces_requests():
    t = {"now": 0.0}
    slept = []

    def sleep(s):
        slept.append(s)
        t["now"] += s

    g = Gate(lambda d: [], Config(delay_min=10, delay_max=10), sleep=sleep, clock=lambda: t["now"])
    g.search("a")
    t["now"] += 3
    g.search("b")
    assert slept == [pytest.approx(7)]
