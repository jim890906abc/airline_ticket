"""機票低價監控主程式。

每次執行的流程：
1. （選用）Travelpayouts：每個月 1 個請求，找出「可能」便宜的日期
2. 挑出本輪要查的日期：可能便宜的優先，其次是最久沒查 / 最近價格接近門檻的日期
3. 用 Google Flights 逐日查詢即時價格，請求之間隨機等待，遇到異常立刻停手
4. 低於門檻且沒通知過（或更便宜了）的班機 → 發 Telegram
"""

from __future__ import annotations

import argparse
import html
import logging
import random
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable

from .config import Config
from .models import Fare
from .state import State
from .telegram import Telegram

log = logging.getLogger("ticket_watch")

TAIPEI = timezone(timedelta(hours=8))
WEEKDAYS = "一二三四五六日"


# ---------------------------------------------------------------- 日期排程


def window_dates(today: date, cfg: Config) -> list[str]:
    return [
        (today + timedelta(days=i)).isoformat()
        for i in range(cfg.days_ahead_min, cfg.days_ahead_max + 1)
    ]


def pick_dates(
    state: State,
    candidates: list[str],
    now: datetime,
    cfg: Config,
    priority: Iterable[str] = (),
    rng: random.Random | None = None,
) -> list[str]:
    """挑選本輪要查的日期。

    - priority（外部來源認為便宜的日期）一定先查
    - 其餘依「多久沒查」排序；從沒查過的最優先
    - 最近價格接近門檻的日期視為「熱門」，過期速度 x3，會更頻繁重查
    """
    rng = rng or random.Random()
    allowed = set(candidates)
    chosen: list[str] = []
    for d in priority:
        if d in allowed and d not in chosen and len(chosen) < cfg.dates_per_run:
            chosen.append(d)

    hot_limit = cfg.max_price * cfg.hot_margin

    def staleness(d: str) -> float:
        info = state.dates.get(d)
        if not info or not info.get("checked_at"):
            return float("inf")
        age = (now - datetime.fromisoformat(info["checked_at"])).total_seconds() / 3600
        price = info.get("min_price")
        if price is not None and price < hot_limit:
            age *= 3
        return age

    rest = [d for d in candidates if d not in chosen]
    rng.shuffle(rest)  # 同分時隨機，避免每次都同一個順序
    rest.sort(key=staleness, reverse=True)
    chosen.extend(rest[: max(0, cfg.dates_per_run - len(chosen))])
    rng.shuffle(chosen)  # 查詢順序也打亂，不要每次都照日期順序掃
    return chosen


# ---------------------------------------------------------------- 價格紀錄


def record_date(state: State, d: str, fares: list[Fare], now: datetime, keep: int = 5) -> None:
    """記錄某日期查到的最便宜幾班，給「最便宜 5 張」等功能使用。"""
    fares = sorted(fares, key=lambda f: f.price)
    cheapest = fares[0] if fares else None
    state.dates[d] = {
        "checked_at": now.isoformat(),
        "min_price": cheapest.price if cheapest else None,
        "airline": cheapest.airline if cheapest else "",
        "link": cheapest.link if cheapest else "",
        "fares": [
            {"p": f.price, "a": f.airline, "dt": f.depart_time, "at": f.arrive_time, "s": f.stops}
            for f in fares[:keep]
        ],
    }


def known_fares(state: State, today: str) -> list[tuple[Fare, str]]:
    """所有已知班機（含查詢時間），依價格排序。"""
    out = []
    for d, info in state.dates.items():
        if d < today:
            continue
        for x in info.get("fares") or []:
            fare = Fare(d, x["p"], x["a"], x["dt"], x["at"], x["s"], "google", info.get("link", ""))
            out.append((fare, info.get("checked_at", "")))
    out.sort(key=lambda t: (t[0].price, t[0].date, t[0].depart_time))
    return out


# ---------------------------------------------------------------- 通知判斷


def new_deals_for_date(state: State, d: str, fares: list[Fare], max_price: int) -> list[Fare]:
    """回傳需要通知的班機（沒通知過，或比上次通知更便宜）。

    價格回漲到門檻以上的班機會從已通知清單移除，之後再降價會重新通知。
    """
    deals = {f.key: f for f in fares if f.price < max_price}
    for key in [k for k in state.notified if k.startswith(d + "|") and k not in deals]:
        del state.notified[key]
    return [
        f
        for f in deals.values()
        if f.key not in state.notified or f.price < state.notified[f.key]
    ]


def fmt_date(d: str) -> str:
    return f"{d}（{WEEKDAYS[date.fromisoformat(d).weekday()]}）"


def format_deals(deals: list[Fare], cfg: Config) -> str:
    lines = [
        f"✈️ <b>便宜機票！{cfg.origin} → {cfg.destination} 單程 &lt; {cfg.max_price:,} {cfg.currency}</b>",
        "",
    ]
    for f in sorted(deals, key=lambda x: (x.price, x.date))[:20]:
        stops = "直飛" if f.stops == 0 else f"轉機 {f.stops} 次"
        times = f.depart_time + (f" → {f.arrive_time}" if f.arrive_time else "")
        lines.append(f"📅 {fmt_date(f.date)}  💰 <b>{f.price:,}</b>")
        lines.append(f"   {html.escape(f.airline)} {times} {stops}".rstrip())
        if f.source == "travelpayouts":
            lines.append("   ⚠️ Aviasales 快取價格（Google 本輪未驗證），請自行確認")
        lines.append(f'   <a href="{html.escape(f.link)}">查看 / 訂票</a>')
    if len(deals) > 20:
        lines.append(f"…另有 {len(deals) - 20} 筆")
    return "\n".join(lines)


def format_summary(state: State, cfg: Config, today: str) -> str:
    known = sorted(
        (
            (v["min_price"], d, v.get("airline", ""))
            for d, v in state.dates.items()
            if d >= today and v.get("min_price") is not None
        ),
    )
    lines = [f"📊 每日摘要 {cfg.origin} → {cfg.destination}（已掌握 {len(state.dates)} 個日期）"]
    if not known:
        lines.append("目前還沒有價格資料")
    for price, d, airline in known[:5]:
        lines.append(f"• {fmt_date(d)} {price:,} {cfg.currency} {html.escape(airline)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 主流程


def run(
    cfg: Config,
    state: State,
    search: Callable[[str], list[Fare]],
    tg: Telegram,
    now: datetime,
    tp_fetch: Callable[[date, date], list[Fare]] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> State:
    from .google_flights import BlockedError

    rng = rng or random.Random()
    today = now.astimezone(TAIPEI).date()
    state.prune(today.isoformat())
    candidates = window_dates(today, cfg)

    # 1) 外部低價快取（選用）
    tp_deals: list[Fare] = []
    priority: list[str] = []
    if tp_fetch is not None:
        start = date.fromisoformat(candidates[0])
        end = date.fromisoformat(candidates[-1])
        tp_fares = tp_fetch(start, end)
        hot_limit = cfg.max_price * cfg.hot_margin
        for f in sorted(tp_fares, key=lambda x: x.price):
            if f.price < hot_limit and f.date not in priority:
                priority.append(f.date)
            if f.price < cfg.max_price:
                tp_deals.append(f)
        log.info("Travelpayouts：%d 筆快取價格，%d 個疑似低價日期", len(tp_fares), len(priority))

    # 2) Google Flights 逐日驗證
    dates = pick_dates(state, candidates, now, cfg, priority=priority, rng=rng)
    log.info("本輪查詢 %d 個日期：%s", len(dates), ", ".join(sorted(dates)))

    to_notify: list[Fare] = []
    verified: set[str] = set()
    attempted = errors_in_row = 0
    for i, d in enumerate(dates):
        if i > 0:
            sleep(rng.uniform(cfg.delay_min, cfg.delay_max))
        attempted += 1
        try:
            fares = search(d)
        except BlockedError as e:
            errors_in_row += 1
            log.warning("%s 查詢失敗（%s）", d, e)
            if errors_in_row >= cfg.max_consecutive_errors or any(
                code in str(e) for code in ("HTTP 429", "HTTP 403")
            ):
                log.warning("疑似被限流，本輪提前結束")
                break
            continue
        errors_in_row = 0
        verified.add(d)
        record_date(state, d, fares, now)
        cheapest = fares[0] if fares else None
        log.info(
            "%s 最低 %s",
            d,
            f"{cheapest.price:,} {cheapest.airline} {cheapest.depart_time}" if cheapest else "無航班",
        )
        to_notify.extend(new_deals_for_date(state, d, fares, cfg.max_price))

    # Google 沒驗證到的日期，退而用快取價格通知
    for f in tp_deals:
        if f.date not in verified and (
            f.key not in state.notified or f.price < state.notified[f.key]
        ):
            to_notify.append(f)

    # 3) 發通知；發送成功才記錄，失敗下輪會重送
    if to_notify:
        log.info("找到 %d 筆新低價，發送通知", len(to_notify))
        if tg.send(format_deals(to_notify, cfg)):
            for f in to_notify:
                state.notified[f.key] = f.price

    # 4) 被擋偵測：連續多輪完全查不到才警告一次，恢復時再通知
    successes = len(verified)
    if attempted and not successes:
        state.fail_streak += 1
        if state.fail_streak >= cfg.block_alert_after and not state.block_warned:
            if tg.send(
                f"⚠️ 機票監控已連續 {state.fail_streak} 輪查不到 Google Flights 資料，"
                "可能被暫時擋下或頁面改版，請檢查 GitHub Actions 紀錄。"
            ):
                state.block_warned = True
    elif successes:
        if state.block_warned:
            tg.send("✅ 機票監控已恢復正常")
        state.fail_streak = 0
        state.block_warned = False

    # 5) 每日摘要
    local = now.astimezone(TAIPEI)
    if (
        cfg.summary_hour >= 0
        and local.hour >= cfg.summary_hour
        and state.last_summary != today.isoformat()
    ):
        if tg.send(format_summary(state, cfg, today.isoformat())):
            state.last_summary = today.isoformat()

    return state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="桃園 → 大阪 低價機票 Telegram 通知")
    parser.add_argument(
        "command",
        nargs="?",
        default="check",
        choices=["check", "bot"],
        help="check = 掃描一輪就結束（預設）；bot = 常駐執行，提供按鈕並定時掃描",
    )
    parser.add_argument("--test-telegram", action="store_true", help="發一則測試訊息")
    parser.add_argument("--date", help="只查單一日期並印出結果（除錯用，不通知）")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
    )
    cfg = Config.from_env()
    tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id, dry_run=cfg.dry_run)

    if args.test_telegram:
        ok = tg.send(
            f"👋 機票監控測試訊息\n{cfg.origin} → {cfg.destination} 單程 &lt; "
            f"{cfg.max_price:,} {cfg.currency} 時會通知你"
        )
        if ok and tg.chat_id:
            log.info("測試訊息已送出（chat id：%s）", tg.chat_id)
        return 0 if ok else 1

    from .google_flights import GoogleFlightsClient

    gf = GoogleFlightsClient(cfg.origin, cfg.destination, cfg.currency, cfg.max_stops)

    if args.date:
        for f in gf.search(args.date):
            print(f.price, f.airline, f.depart_time, "->", f.arrive_time, f"stops={f.stops}")
        return 0

    tp_fetch = None
    if cfg.travelpayouts_token:
        from .travelpayouts import fetch_cheapest

        def tp_fetch(start: date, end: date) -> list[Fare]:
            return fetch_cheapest(
                cfg.travelpayouts_token,
                cfg.origin,
                cfg.destination,
                cfg.currency,
                start,
                end,
                direct_only=cfg.max_stops == 0,
            )

    if args.command == "bot":
        return run_bot(cfg, tg, gf.search, tp_fetch)

    state = State.load(cfg.state_file)
    state = run(cfg, state, gf.search, tg, datetime.now(timezone.utc), tp_fetch=tp_fetch)
    state.save(cfg.state_file)
    return 0


def run_bot(cfg: Config, tg: Telegram, search, tp_fetch) -> int:
    import fcntl
    import os

    from .bot import Bot

    if not cfg.telegram_token or not tg.chat_id:
        log.error("bot 模式需要 TELEGRAM_BOT_TOKEN 與 TELEGRAM_CHAT_ID")
        return 1
    # 同時只允許一個 bot 在跑（cron 每 5 分鐘呼叫一次當作看門狗）
    os.makedirs(os.path.dirname(cfg.state_file) or ".", exist_ok=True)
    lock = open(os.path.join(os.path.dirname(cfg.state_file) or ".", "bot.lock"), "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # 已經有一個在跑了
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))  # 給 install.sh 更新時重啟用
    lock.flush()
    Bot(cfg, tg, search, tp_fetch=tp_fetch).run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
