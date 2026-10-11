"""背景掃描：依照網站上設定的通知規則，挑日期查價、判斷是否要發 Telegram。

不太激進的做法：
- 只查「啟用中的通知規則」涵蓋的航線與日期，沒規則就不查
- 每輪只查少量日期（預設 12），從沒查過 / 最久沒查的優先，價格接近門檻的更常重查
- 連續失敗或遇到 429/403 立刻停止本輪
"""

from __future__ import annotations

import html
import logging
import random
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable

from .config import Config
from .google_flights import BlockedError
from .models import Fare, route_label
from .rules import Rule
from .state import State, split_key

log = logging.getLogger("ticket_watch.scanner")

TAIPEI = timezone(timedelta(hours=8))
WEEKDAYS = "一二三四五六日"

Search = Callable[[str, str, str], list[Fare]]
Target = tuple[str, str]  # (route, date)


def taipei_today(now: datetime) -> date:
    return now.astimezone(TAIPEI).date()


def fmt_date(d: str) -> str:
    return f"{d}（{WEEKDAYS[date.fromisoformat(d).weekday()]}）"


# ---------------------------------------------------------------- 要查哪些日期


def watch_targets(rules: list[Rule], today: date) -> dict[Target, int]:
    """所有啟用規則涵蓋的 (航線, 日期) → 該日期最寬鬆的門檻。"""
    out: dict[Target, int] = {}
    for r in rules:
        w = r.window(today)
        if not w:
            continue
        d = w[0]
        while d <= w[1]:
            if r.covers(r.route, d.isoformat(), today):
                key = (r.route, d.isoformat())
                out[key] = max(out.get(key, 0), r.max_price)
            d += timedelta(days=1)
    return out


def pick_targets(
    state: State,
    targets: dict[Target, int],
    now: datetime,
    n: int,
    hot_margin: float,
    priority: Iterable[Target] = (),
    rng: random.Random | None = None,
) -> list[Target]:
    rng = rng or random.Random()
    chosen: list[Target] = []
    for t in priority:
        if t in targets and t not in chosen and len(chosen) < n:
            chosen.append(t)

    def staleness(t: Target) -> float:
        info = state.info(*t)
        if not info or not info.get("checked_at"):
            return float("inf")
        age = (now - datetime.fromisoformat(info["checked_at"])).total_seconds() / 3600
        price = info.get("min_price")
        if price is not None and price < targets[t] * hot_margin:
            age *= 3
        return age

    rest = [t for t in targets if t not in chosen]
    rng.shuffle(rest)
    rest.sort(key=staleness, reverse=True)
    chosen.extend(rest[: max(0, n - len(chosen))])
    rng.shuffle(chosen)
    return chosen


# ---------------------------------------------------------------- 通知判斷


def new_deals(state: State, route: str, d: str, fares: list[Fare], rules: list[Rule], today: date) -> list[Fare]:
    """符合任一規則、且沒通知過（或更便宜了）的班機。

    不再符合的班機會從已通知清單移除，之後再降價會重新通知。
    """
    deals = {f.key: f for f in fares if any(r.matches(f, today) for r in rules)}
    prefix = f"{route}|{d}|"
    for key in [k for k in state.notified if k.startswith(prefix) and k not in deals]:
        del state.notified[key]
    return [f for f in deals.values() if f.key not in state.notified or f.price < state.notified[f.key]]


def format_deals(deals: list[Fare]) -> str:
    lines = ["✈️ <b>便宜機票通知</b>", ""]
    for route in sorted({f.route for f in deals}):
        group = sorted((f for f in deals if f.route == route), key=lambda x: (x.price, x.date))
        lines.append(f"<b>【{route_label(group[0].origin, group[0].destination)}】</b>")
        for f in group[:15]:
            stops = "直飛" if f.stops == 0 else f"轉機 {f.stops} 次"
            times = f.depart_time + (f" → {f.arrive_time}" if f.arrive_time else "")
            lines.append(f"📅 {fmt_date(f.date)}  💰 <b>NT${f.price:,}</b>")
            lines.append(f"   {html.escape(f.airline)} {times} {stops}".rstrip())
            if f.source == "travelpayouts":
                lines.append("   ⚠️ Aviasales 快取價格，請自行確認")
            lines.append(f"   🔗 {html.escape(f.link)}")
        if len(group) > 15:
            lines.append(f"…另有 {len(group) - 15} 筆")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_summary(state: State, rules: list[Rule], today: date) -> str:
    lines = ["📊 <b>每日摘要</b>（通知規則範圍內的最低價）"]
    for route in sorted({r.route for r in rules}):
        best: list[tuple[int, str, str]] = []
        for key, info in state.dates.items():
            r, d = split_key(key)
            if r == route and info.get("min_price") is not None and any(x.covers(r, d, today) for x in rules):
                best.append((info["min_price"], d, info.get("airline", "")))
        origin, _, destination = route.partition("-")
        lines.append(f"\n<b>【{route_label(origin, destination)}】</b>")
        if not best:
            lines.append("還沒有資料")
        for p, d, airline in sorted(best)[:3]:
            lines.append(f"• {fmt_date(d)} NT${p:,} {html.escape(airline)}")
    return "\n".join(lines)


def deliver(deals: list[Fare], notifier, state: State) -> None:
    """發送通知；成功才標記為已通知，失敗下次會重送。"""
    if not deals:
        return
    log.info("找到 %d 筆新低價，發送通知", len(deals))
    if notifier.send(format_deals(deals)):
        for f in deals:
            state.notified[f.key] = f.price


# ---------------------------------------------------------------- 一輪掃描


def scan_round(
    cfg: Config,
    state: State,
    rules: list[Rule],
    dates_per_run: int,
    search: Search,
    notifier,
    now: datetime,
    tp_fetch: Callable[[str, str, date, date], list[Fare]] | None = None,
    rng: random.Random | None = None,
    on_progress: Callable[[int, int, Target, list[Fare] | None], None] | None = None,
    should_stop: Callable[[], bool] = lambda: False,
    commit: Callable[[State], None] | None = None,
) -> dict:
    """掃描一輪。回傳 {"planned", "checked", "failed", "aborted"}。

    commit：每查完一個日期就呼叫，讓網站能即時看到新資料。
    """
    rng = rng or random.Random()
    today = taipei_today(now)
    state.prune(today.isoformat())
    targets = watch_targets(rules, today)
    stats = {"planned": 0, "checked": 0, "failed": 0, "aborted": False}
    if not targets:
        log.info("沒有啟用中的通知規則，略過掃描")
        return stats

    # 1) （選用）Travelpayouts 找出可能便宜的日期
    tp_deals: list[Fare] = []
    priority: list[Target] = []
    if tp_fetch is not None:
        for route in sorted({t[0] for t in targets}):
            origin, _, destination = route.partition("-")
            ds = sorted(d for r, d in targets if r == route)
            try:
                fares = tp_fetch(origin, destination, date.fromisoformat(ds[0]), date.fromisoformat(ds[-1]))
            except Exception as e:
                log.warning("Travelpayouts 失敗：%s", e)
                continue
            for f in sorted(fares, key=lambda x: x.price):
                t = (f.route, f.date)
                if t in targets and f.price < targets[t] * cfg.hot_margin and t not in priority:
                    priority.append(t)
                if any(r.matches(f, today) for r in rules):
                    tp_deals.append(f)

    # 2) Google Flights 驗證
    plan = pick_targets(state, targets, now, dates_per_run, cfg.hot_margin, priority, rng)
    stats["planned"] = len(plan)
    log.info("本輪查詢 %d 個日期", len(plan))
    to_notify: list[Fare] = []
    verified: set[Target] = set()
    errors_in_row = 0
    for i, (route, d) in enumerate(plan):
        if should_stop():
            stats["aborted"] = True
            break
        origin, _, destination = route.partition("-")
        try:
            fares = search(origin, destination, d)
        except BlockedError as e:
            stats["failed"] += 1
            errors_in_row += 1
            log.warning("%s %s 查詢失敗（%s）", route, d, e)
            if on_progress:
                on_progress(i + 1, len(plan), (route, d), None)
            if errors_in_row >= cfg.max_consecutive_errors or any(c in str(e) for c in ("HTTP 429", "HTTP 403")):
                log.warning("疑似被限流，本輪提前結束")
                stats["aborted"] = True
                break
            continue
        errors_in_row = 0
        verified.add((route, d))
        stats["checked"] += 1
        state.record(route, d, fares, now)
        to_notify.extend(new_deals(state, route, d, fares, rules, today))
        log.info("%s %s 最低 %s", route, d, f"{fares[0].price:,} {fares[0].airline}" if fares else "無航班")
        if commit:
            commit(state)
        if on_progress:
            on_progress(i + 1, len(plan), (route, d), fares)

    for f in tp_deals:
        if (f.route, f.date) not in verified and (f.key not in state.notified or f.price < state.notified[f.key]):
            to_notify.append(f)
    deliver(to_notify, notifier, state)

    # 3) 被擋偵測
    if plan and not verified and not (stats["aborted"] and should_stop()):
        state.fail_streak += 1
        if state.fail_streak >= cfg.block_alert_after and not state.block_warned:
            if notifier.send(
                f"⚠️ 機票監控已連續 {state.fail_streak} 輪查不到 Google Flights 資料，"
                "可能被暫時擋下或頁面改版，請到網站或 log 查看。"
            ):
                state.block_warned = True
    elif verified:
        if state.block_warned:
            notifier.send("✅ 機票監控已恢復正常")
        state.fail_streak = 0
        state.block_warned = False

    # 4) 每日摘要
    local = now.astimezone(TAIPEI)
    if cfg.summary_hour >= 0 and local.hour >= cfg.summary_hour and state.last_summary != today.isoformat():
        if notifier.send(format_summary(state, rules, today)):
            state.last_summary = today.isoformat()

    state.last_scan_at = now.isoformat()
    return stats
