import json
import random
from datetime import date, datetime, timedelta, timezone

import pytest

from ticket_watch.config import Config
from ticket_watch.google_flights import BlockedError, parse_fares
from ticket_watch.models import Fare
from ticket_watch.rules import Rule, Settings
from ticket_watch.scanner import new_deals, pick_targets, scan_round, watch_targets
from ticket_watch.state import State
from ticket_watch.travelpayouts import months_between

NOW = datetime(2026, 10, 11, 2, 0, tzinfo=timezone.utc)  # 台北 10:00
TODAY = date(2026, 10, 11)


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


def fare(d, price, airline="台灣虎航", dep="07:00", route="TPE-KIX"):
    o, _, dst = route.partition("-")
    return Fare(d, price, airline, dep, "10:30", 0, "google", "https://x", o, dst)


class FakeTG:
    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    def send(self, text, reply_markup=None):
        self.sent.append(text)
        return self.ok


def cfg(**kw):
    base = dict(summary_hour=-1)
    base.update(kw)
    return Config(**base)


def rule(route="TPE-KIX", **kw):
    o, _, d = route.partition("-")
    base = dict(id="r1", origin=o, destination=d, rolling_days=30, max_price=3500)
    base.update(kw)
    return Rule(**base)


def scan(state, search, tg=None, rules=None, n=5, **kw):
    return scan_round(cfg(), state, rules or [rule()], n, search, tg or FakeTG(), NOW, **kw)


# ---------------------------------------------------------------- parser


def test_parse_merges_best_and_other_sections():
    html = page(
        best=[item(3299, "IT", [segment([7], [10, 30])])],
        other=[
            item(4100, "MM", [segment([None, 45], [4, 15])]),
            item(3299, "IT", [segment([7], [10, 30])]),
        ],
    )
    fares = parse_fares(html, date="2026-11-18", link="L", origin="KIX", destination="TPE")
    assert [(f.price, f.airline, f.depart_time, f.arrive_time) for f in fares] == [
        (3299, "台灣虎航", "07:00", "10:30"),
        (4100, "樂桃航空", "00:45", "04:15"),
    ]
    assert all(f.route == "KIX-TPE" and f.link == "L" for f in fares)


def test_parse_counts_stops():
    html = page(other=[item(5000, "IT", [segment([7], [9], to="OKA"), segment([11], [13], frm="OKA")])])
    (f,) = parse_fares(html, date="2026-11-18", link="L")
    assert f.stops == 1 and f.arrive_time == "13:00"


def test_parse_empty_and_blocked():
    assert parse_fares(page(), date="2026-11-18", link="L") == []
    with pytest.raises(BlockedError):
        parse_fares("<html>Before you continue to Google</html>", date="2026-11-18", link="L")


# ---------------------------------------------------------------- rules


def test_rule_window_rolling_and_fixed():
    r = rule(rolling_days=10)
    assert r.window(TODAY) == (date(2026, 10, 12), date(2026, 10, 21))
    r = rule(date_from="2026-12-20", date_to="2027-01-05")
    assert r.window(TODAY) == (date(2026, 12, 20), date(2027, 1, 5))
    assert rule(date_from="2026-01-01", date_to="2026-02-01").window(TODAY) is None  # 已過期
    assert rule(date_from="2026-12-01", date_to="2028-01-01").window(TODAY)[1] == TODAY + timedelta(days=330)


def test_rule_matches_route_price_weekday():
    r = rule(date_from="2026-11-01", date_to="2026-11-30", weekdays=[4, 5])  # 週五六
    assert r.matches(fare("2026-11-20", 3400), TODAY)  # 週五
    assert not r.matches(fare("2026-11-18", 3400), TODAY)  # 週三
    assert not r.matches(fare("2026-11-20", 3500), TODAY)  # 不低於門檻
    assert not r.matches(fare("2026-11-20", 3000, route="KIX-TPE"), TODAY)  # 方向不同
    assert not r.matches(fare("2026-12-04", 3000), TODAY)  # 區間外


def test_rule_validation():
    with pytest.raises(ValueError):
        Rule.from_dict({"origin": "TPE", "destination": "NRT"}).validate()
    with pytest.raises(ValueError):
        Rule.from_dict({"date_from": "2026-12-01", "date_to": "2026-11-01"}).validate()
    with pytest.raises(ValueError):
        Rule.from_dict({"max_price": 10}).validate()


def test_settings_persist_and_migrate_old_bot_settings(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"max_price": 4200, "paused": True}))  # 舊版 bot 格式
    s = Settings(str(p))
    assert s.paused and len(s.rules) == 1 and s.rules[0].max_price == 4200
    r = s.upsert_rule({"origin": "KIX", "destination": "TPE", "max_price": 3000, "name": "回程"})
    assert r.id and len(Settings(str(p)).rules) == 2
    s.upsert_rule({**r.__dict__, "max_price": 2800})
    assert [x.max_price for x in Settings(str(p)).rules] == [4200, 2800]
    assert s.delete_rule(r.id) and len(Settings(str(p)).rules) == 1
    fresh = Settings(str(tmp_path / "none.json"), default_max_price=3500)
    assert fresh.rules[0].route == "TPE-KIX" and fresh.rules[0].max_price == 3500


# ---------------------------------------------------------------- 排程


def test_watch_targets_union_with_loosest_threshold():
    rules = [
        rule(date_from="2026-11-01", date_to="2026-11-03", max_price=3000),
        rule(id="r2", date_from="2026-11-03", date_to="2026-11-04", max_price=4000),
        rule(id="r3", route="KIX-TPE", date_from="2026-11-05", date_to="2026-11-05"),
    ]
    t = watch_targets(rules, TODAY)
    assert t == {
        ("TPE-KIX", "2026-11-01"): 3000,
        ("TPE-KIX", "2026-11-02"): 3000,
        ("TPE-KIX", "2026-11-03"): 4000,
        ("TPE-KIX", "2026-11-04"): 4000,
        ("KIX-TPE", "2026-11-05"): 3500,
    }


def test_pick_targets_prefers_unchecked_stale_and_priority():
    targets = {("TPE-KIX", f"2026-11-{i:02d}"): 3500 for i in range(1, 21)}
    st = State()
    for r, d in targets:
        st.record(r, d, [fare(d, 9000)], NOW)
    st.dates["TPE-KIX|2026-11-10"]["checked_at"] = (NOW - timedelta(hours=10)).isoformat()
    del st.dates["TPE-KIX|2026-11-15"]
    got = pick_targets(st, targets, NOW, 3, 1.3, priority=[("TPE-KIX", "2026-11-05"), ("X", "y")], rng=random.Random(1))
    assert set(got) == {("TPE-KIX", "2026-11-05"), ("TPE-KIX", "2026-11-15"), ("TPE-KIX", "2026-11-10")}


def test_hot_dates_expire_faster():
    targets = {("TPE-KIX", "2026-11-01"): 3500, ("TPE-KIX", "2026-11-02"): 3500}
    st = State()
    st.record("TPE-KIX", "2026-11-01", [fare("2026-11-01", 9000)], NOW - timedelta(hours=5))
    st.record("TPE-KIX", "2026-11-02", [fare("2026-11-02", 3800)], NOW - timedelta(hours=3))
    assert pick_targets(st, targets, NOW, 1, 1.3, rng=random.Random(0)) == [("TPE-KIX", "2026-11-02")]


# ---------------------------------------------------------------- 通知判斷


def test_dedup_and_renotify():
    s, d, rules = State(), "2026-10-20", [rule()]
    assert len(new_deals(s, "TPE-KIX", d, [fare(d, 3400), fare(d, 3600, dep="12:00")], rules, TODAY)) == 1
    s.notified[fare(d, 3400).key] = 3400
    assert new_deals(s, "TPE-KIX", d, [fare(d, 3400)], rules, TODAY) == []
    assert len(new_deals(s, "TPE-KIX", d, [fare(d, 3200)], rules, TODAY)) == 1
    assert new_deals(s, "TPE-KIX", d, [fare(d, 3900)], rules, TODAY) == []
    assert s.notified == {}


def test_scan_notifies_both_routes_in_one_message():
    rules = [rule(rolling_days=3), rule(id="r2", route="KIX-TPE", rolling_days=3, max_price=4000)]

    def search(o, dst, d):
        return [fare(d, 3800 if o == "KIX" else 3600, route=f"{o}-{dst}")]

    tg = FakeTG()
    st = State()
    stats = scan(st, search, tg, rules=rules, n=10)
    assert stats["checked"] == 6 and len(tg.sent) == 1
    assert "【大阪關西 → 桃園】" in tg.sent[0] and "【桃園 → 大阪關西】" not in tg.sent[0]
    assert len(st.notified) == 3 and st.last_scan_at == NOW.isoformat()
    scan(st, search, tg, rules=rules, n=10)
    assert len(tg.sent) == 1  # 不重複


def test_scan_without_rules_does_nothing():
    calls = []
    stats = scan_round(cfg(), State(), [], 5, lambda *a: calls.append(a) or [], FakeTG(), NOW)
    assert stats["planned"] == 0 and calls == []


def test_scan_send_failure_not_marked():
    st = State()
    scan(st, lambda o, dst, d: [fare(d, 3000)], FakeTG(ok=False), n=1)
    assert st.notified == {}


def test_scan_stops_on_errors_and_warns_once():
    c = cfg(max_consecutive_errors=2, block_alert_after=2)
    calls = []

    def search(o, dst, d):
        calls.append(d)
        raise BlockedError("flight data block not found")

    tg, st = FakeTG(), State()
    scan_round(c, st, [rule()], 6, search, tg, NOW)
    assert len(calls) == 2 and st.fail_streak == 1 and tg.sent == []
    scan_round(c, st, [rule()], 6, search, tg, NOW)
    assert st.block_warned and len(tg.sent) == 1
    scan_round(c, st, [rule()], 6, search, tg, NOW)
    assert len(tg.sent) == 1
    scan_round(c, st, [rule()], 6, lambda *a: [], tg, NOW)
    assert st.fail_streak == 0 and "恢復" in tg.sent[-1]


def test_scan_stops_immediately_on_429():
    calls = []

    def search(o, dst, d):
        calls.append(d)
        raise BlockedError("HTTP 429")

    scan(State(), search)
    assert len(calls) == 1


def test_travelpayouts_priority_and_fallback():
    tp = [Fare("2026-10-25", 3000, "IT", "07:00", "", 0, "travelpayouts", "https://a", "TPE", "KIX")]
    checked = []

    def search(o, dst, d):
        checked.append(d)
        return [fare(d, 3300)]

    tg = FakeTG()
    scan(State(), search, tg, n=1, tp_fetch=lambda *a: tp)
    assert checked == ["2026-10-25"] and "3,300" in tg.sent[0] and "快取" not in tg.sent[0]

    def broken(o, dst, d):
        raise BlockedError("HTTP 429")

    tg = FakeTG()
    scan(State(), broken, tg, n=1, tp_fetch=lambda *a: tp)
    assert "3,000" in tg.sent[0] and "快取" in tg.sent[0]


def test_daily_summary_once_per_day():
    tg, st = FakeTG(), State()
    c = cfg(summary_hour=9)
    scan_round(c, st, [rule()], 2, lambda o, dst, d: [fare(d, 5000)], tg, NOW)
    assert len(tg.sent) == 1 and "每日摘要" in tg.sent[0]
    scan_round(c, st, [rule()], 2, lambda o, dst, d: [fare(d, 5000)], tg, NOW)
    assert len(tg.sent) == 1


# ---------------------------------------------------------------- state


def test_state_roundtrip_prune_and_legacy_migration(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(
        json.dumps(
            {
                "dates": {"2026-10-01": {}, "2026-12-01": {"min_price": 1, "checked_at": "x", "fares": []}},
                "notified": {"2026-10-01|a|b": 1, "2026-12-01|a|b": 2},
            }
        )
    )
    s = State.load(str(p))
    assert set(s.dates) == {"TPE-KIX|2026-10-01", "TPE-KIX|2026-12-01"}
    assert set(s.notified) == {"TPE-KIX|2026-10-01|a|b", "TPE-KIX|2026-12-01|a|b"}
    s.prune("2026-10-11")
    s.save(str(p))
    s2 = State.load(str(p))
    assert list(s2.dates) == ["TPE-KIX|2026-12-01"] and list(s2.notified) == ["TPE-KIX|2026-12-01|a|b"]
    assert State.load(str(tmp_path / "missing.json")).dates == {}


def test_state_known_fares():
    s = State()
    s.record("TPE-KIX", "2026-11-01", [fare("2026-11-01", 4000), fare("2026-11-01", 3000, dep="20:00")], NOW)
    s.record("KIX-TPE", "2026-11-01", [fare("2026-11-01", 1000, route="KIX-TPE")], NOW)
    got = s.known_fares("TPE-KIX", "2026-10-01", "2026-12-31")
    assert [f.price for f, _ in got] == [3000, 4000] and got[0][0].route == "TPE-KIX"


def test_months_between():
    assert months_between(date(2026, 11, 20), date(2027, 2, 1)) == ["2026-11", "2026-12", "2027-01", "2027-02"]
