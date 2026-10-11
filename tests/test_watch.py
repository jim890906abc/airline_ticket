import json
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from ticket_watch.config import Config
from ticket_watch.google_flights import BlockedError, parse_fares
from ticket_watch.main import TAIPEI, new_deals_for_date, pick_dates, run, window_dates
from ticket_watch.models import Fare
from ticket_watch.state import State
from ticket_watch.travelpayouts import months_between


# ---------------------------------------------------------------- helpers


def segment(dep, arr, frm="TPE", to="KIX", d=(2026, 11, 18)):
    seg = [None] * 22
    seg[3], seg[4], seg[5], seg[6] = frm, "Taoyuan", "Kansai", to
    seg[8], seg[10], seg[11], seg[17] = dep, arr, 150, "A320"
    seg[20], seg[21] = list(d), list(d)
    return seg


def item(price, airline, segs):
    flight = [None] * 23
    flight[0], flight[1], flight[2] = "IT", [airline], segs
    flight[22] = [None] * 9
    return [flight, [[None, price]]]


def page(best=None, other=None):
    payload = [None] * 8
    payload[2] = [best] if best is not None else None
    payload[3] = [other] if other is not None else None
    payload[7] = [None, [[["SKYTEAM", "SkyTeam"]], [["IT", "台灣虎航"], ["MM", "樂桃航空"]]]]
    js = "AF_initDataCallback({key: 'ds:1', hash: '1', data:" + json.dumps(payload) + ", sideChannel: {}});"
    return f'<html><body><script class="ds:1">{js}</script></body></html>'


def fare(d, price, airline="台灣虎航", dep="07:00"):
    return Fare(d, price, airline, dep, "10:30", 0, "google", "https://x")


class FakeTG:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    def send(self, text):
        self.sent.append(text)
        return self.ok


NOW = datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc)  # 台北 10:00


def cfg(**kw):
    base = dict(dates_per_run=5, days_ahead_max=30, summary_hour=-1)
    base.update(kw)
    return Config(**base)


# ---------------------------------------------------------------- parser


def test_parse_merges_best_and_other_sections():
    html = page(
        best=[item(3299, "IT", [segment([7], [10, 30])])],
        other=[
            item(4100, "MM", [segment([None, 45], [4, 15])]),
            item(3299, "IT", [segment([7], [10, 30])]),  # 重複出現
        ],
    )
    fares = parse_fares(html, date="2026-11-18", link="L")
    assert [(f.price, f.airline, f.depart_time, f.arrive_time) for f in fares] == [
        (3299, "台灣虎航", "07:00", "10:30"),
        (4100, "樂桃航空", "00:45", "04:15"),
    ]
    assert all(f.stops == 0 and f.link == "L" for f in fares)


def test_parse_counts_stops():
    html = page(other=[item(5000, "IT", [segment([7], [9], to="OKA"), segment([11], [13], frm="OKA")])])
    (f,) = parse_fares(html, date="2026-11-18", link="L")
    assert f.stops == 1 and f.arrive_time == "13:00"


def test_parse_empty_results():
    assert parse_fares(page(), date="2026-11-18", link="L") == []


def test_parse_consent_page_is_blocked():
    with pytest.raises(BlockedError):
        parse_fares("<html>Before you continue to Google</html>", date="2026-11-18", link="L")


# ---------------------------------------------------------------- scheduling


def test_pick_dates_prefers_unchecked_then_stale_and_priority():
    c = cfg(dates_per_run=3)
    today = NOW.astimezone(TAIPEI).date()
    cands = window_dates(today, c)
    state = State()
    for d in cands:
        state.dates[d] = {"checked_at": NOW.isoformat(), "min_price": 9000}
    old = cands[10]
    state.dates[old]["checked_at"] = (NOW - timedelta(hours=10)).isoformat()
    del state.dates[cands[20]]  # 從未查過

    got = pick_dates(state, cands, NOW, c, priority=[cands[5], "1999-01-01"], rng=random.Random(1))
    assert set(got) == {cands[5], cands[20], old}


def test_hot_dates_expire_faster():
    c = cfg(dates_per_run=1)
    cands = ["2026-11-01", "2026-11-02"]
    state = State(
        dates={
            "2026-11-01": {"checked_at": (NOW - timedelta(hours=5)).isoformat(), "min_price": 9000},
            "2026-11-02": {"checked_at": (NOW - timedelta(hours=3)).isoformat(), "min_price": 3800},
        }
    )
    assert pick_dates(state, cands, NOW, c, rng=random.Random(0)) == ["2026-11-02"]


# ---------------------------------------------------------------- dedup


def test_dedup_and_renotify_on_lower_price_or_redrop():
    s = State()
    d = "2026-11-18"
    assert len(new_deals_for_date(s, d, [fare(d, 3400), fare(d, 3600, dep="12:00")], 3500)) == 1
    s.notified[fare(d, 3400).key] = 3400
    assert new_deals_for_date(s, d, [fare(d, 3400)], 3500) == []  # 同價不重複
    assert len(new_deals_for_date(s, d, [fare(d, 3200)], 3500)) == 1  # 更便宜再通知
    assert new_deals_for_date(s, d, [fare(d, 3900)], 3500) == []  # 回漲 → 移除紀錄
    assert s.notified == {}
    assert len(new_deals_for_date(s, d, [fare(d, 3400)], 3500)) == 1  # 再降 → 再通知


def test_threshold_is_strictly_below():
    assert new_deals_for_date(State(), "2026-11-18", [fare("2026-11-18", 3500)], 3500) == []


# ---------------------------------------------------------------- run()


def test_run_notifies_once_and_records_state():
    c = cfg(dates_per_run=30)  # 一輪掃完整個視窗
    cheap = "2026-10-20"

    def search(d):
        return [fare(d, 3100)] if d == cheap else [fare(d, 4500)]

    tg = FakeTG()
    sleeps = []
    s = run(c, State(), search, tg, NOW, sleep=sleeps.append, rng=random.Random(2))
    assert len(tg.sent) == 1 and "3,100" in tg.sent[0] and "2026-10-20（二）" in tg.sent[0]
    assert len(s.dates) == 30 and len(sleeps) == 29
    assert all(c.delay_min <= x <= c.delay_max for x in sleeps)

    # 下一輪同樣價格 → 不重複通知
    run(c, s, search, tg, NOW, sleep=lambda _: None)
    assert len(tg.sent) == 1


def test_run_does_not_mark_notified_when_send_fails():
    c = cfg(dates_per_run=1)
    s = run(c, State(), lambda d: [fare(d, 3000)], FakeTG(ok=False), NOW, sleep=lambda _: None)
    assert s.notified == {}


def test_run_stops_after_consecutive_errors_and_warns_once():
    c = cfg(dates_per_run=6, max_consecutive_errors=2, block_alert_after=2)
    calls = []

    def search(d):
        calls.append(d)
        raise BlockedError("flight data block not found")

    tg = FakeTG()
    s = run(c, State(), search, tg, NOW, sleep=lambda _: None)
    assert len(calls) == 2 and s.fail_streak == 1 and tg.sent == []
    s = run(c, s, search, tg, NOW, sleep=lambda _: None)
    assert s.fail_streak == 2 and s.block_warned and len(tg.sent) == 1
    run(c, s, search, tg, NOW, sleep=lambda _: None)
    assert len(tg.sent) == 1  # 不重複警告
    s = run(c, s, lambda d: [], tg, NOW, sleep=lambda _: None)
    assert s.fail_streak == 0 and not s.block_warned and "恢復" in tg.sent[-1]


def test_run_stops_immediately_on_429():
    calls = []

    def search(d):
        calls.append(d)
        raise BlockedError("HTTP 429")

    run(cfg(), State(), search, FakeTG(), NOW, sleep=lambda _: None)
    assert len(calls) == 1


def test_travelpayouts_dates_get_priority_and_fallback_alert():
    c = cfg(dates_per_run=1)
    tp = [Fare("2026-10-25", 3000, "IT", "07:00", "", 0, "travelpayouts", "https://a")]
    checked = []

    def search(d):
        checked.append(d)
        return [fare(d, 3300)]

    tg = FakeTG()
    s = run(c, State(), search, tg, NOW, tp_fetch=lambda a, b: tp, sleep=lambda _: None)
    assert checked == ["2026-10-25"]
    # Google 已驗證該日期 → 以 Google 結果為準，不發快取價格
    assert "3,300" in tg.sent[0] and "快取" not in tg.sent[0]

    # Google 失敗時 → 退而通知快取價格
    def broken(d):
        raise BlockedError("HTTP 429")

    tg = FakeTG()
    run(c, State(), broken, tg, NOW, tp_fetch=lambda a, b: tp, sleep=lambda _: None)
    assert "3,000" in tg.sent[0] and "快取" in tg.sent[0]


def test_daily_summary_once_per_day():
    c = cfg(summary_hour=9, dates_per_run=2)
    tg = FakeTG()
    s = run(c, State(), lambda d: [fare(d, 5000)], tg, NOW, sleep=lambda _: None)
    assert len(tg.sent) == 1 and "每日摘要" in tg.sent[0]
    run(c, s, lambda d: [fare(d, 5000)], tg, NOW, sleep=lambda _: None)
    assert len(tg.sent) == 1


def test_state_roundtrip_and_prune(tmp_path):
    p = str(tmp_path / "s" / "state.json")
    s = State(
        dates={"2026-10-01": {}, "2026-12-01": {"min_price": 1}},
        notified={"2026-10-01|a|b": 1, "2026-12-01|a|b": 2},
    )
    s.prune("2026-10-11")
    s.save(p)
    s2 = State.load(p)
    assert s2.dates == {"2026-12-01": {"min_price": 1}}
    assert s2.notified == {"2026-12-01|a|b": 2}
    assert State.load(str(tmp_path / "missing.json")).dates == {}


def test_months_between():
    assert months_between(date(2026, 11, 20), date(2027, 2, 1)) == [
        "2026-11",
        "2026-12",
        "2027-01",
        "2027-02",
    ]
