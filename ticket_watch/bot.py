"""互動式 Telegram bot：常駐執行，同時負責「按鈕操作」與「定時掃描」。

- 主執行緒：long polling 接收 Telegram 訊息 / 按鈕（只回應設定的 chat id）
- 背景執行緒：依序處理工作（定時掃描、立即掃描、查指定日期、重查）
  所有 Google 請求都只在這個執行緒發出，並經過 Gate 統一隨機間隔，不會因為狂按按鈕就爆量
"""

from __future__ import annotations

import copy
import dataclasses
import html
import json
import logging
import os
import queue
import random
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from .config import Config
from .google_flights import BlockedError
from .main import TAIPEI, fmt_date, known_fares, new_deals_for_date, record_date, run
from .models import Fare
from .state import State
from .telegram import MutedTelegram, Telegram

log = logging.getLogger("ticket_watch.bot")

BTN_CHEAPEST = "🔥 最便宜5張"
BTN_MONTHS = "📆 各月最低價"
BTN_DATE = "📅 查指定日期"
BTN_SCAN = "🔍 立即掃描"
BTN_THRESHOLD = "💰 設定門檻"
BTN_PAUSE = "⏸ 暫停通知"
BTN_RESUME = "▶️ 恢復通知"
BTN_STATUS = "ℹ️ 狀態"

COMMANDS = [
    ("menu", "顯示按鈕選單"),
    ("cheapest", "目前最便宜 5 張"),
    ("months", "各月最低價"),
    ("date", "查指定日期，例：/date 11/18"),
    ("scan", "立即掃描一輪"),
    ("threshold", "設定通知門檻"),
    ("pause", "暫停通知"),
    ("resume", "恢復通知"),
    ("status", "系統狀態"),
]

THRESHOLD_PRESETS = [3000, 3500, 4000, 4500, 5000]
MAX_DAYS_AHEAD = 330  # Google Flights 大約只開放到 330 天後
SCAN_COOLDOWN = timedelta(minutes=20)  # 手動掃描冷卻，避免狂按


# ---------------------------------------------------------------- 小工具


def parse_date(text: str, today: date) -> date | None:
    """支援 2026-11-18、2026/11/18、11/18、11-18（自動推算年份）。"""
    t = text.strip()
    try:
        m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t)
        if m:
            return date(int(m[1]), int(m[2]), int(m[3]))
        m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})", t)
        if m:
            d = date(today.year, int(m[1]), int(m[2]))
            return d if d >= today else date(today.year + 1, int(m[1]), int(m[2]))
    except ValueError:
        return None
    return None


def ago(iso: str, now: datetime) -> str:
    if not iso:
        return "未知"
    mins = int((now - datetime.fromisoformat(iso)).total_seconds() // 60)
    if mins < 1:
        return "剛剛"
    if mins < 60:
        return f"{mins} 分鐘前"
    if mins < 60 * 24:
        return f"{mins // 60} 小時前"
    return f"{mins // (60 * 24)} 天前"


def fare_lines(i: int, f: Fare, max_price: int, checked: str = "", now: datetime | None = None) -> list[str]:
    fire = "🔥" if f.price < max_price else ""
    stops = "直飛" if f.stops == 0 else f"轉 {f.stops} 次"
    times = f.depart_time + (f"→{f.arrive_time}" if f.arrive_time else "")
    head = f"{i}. {fmt_date(f.date)} <b>NT${f.price:,}</b>{fire}"
    body = f"   {html.escape(f.airline)} {times} {stops}"
    if checked and now:
        body += f"（{ago(checked, now)}查）"
    lines = [head, body]
    if f.link:
        lines.append(f"   🔗 {html.escape(f.link)}")
    return lines


class Settings:
    """使用者在 bot 裡改的設定（門檻、暫停），獨立存檔。"""

    def __init__(self, path: str, default_max_price: int):
        self.path = path
        self.max_price = default_max_price
        self.paused = False
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            self.max_price = int(raw.get("max_price") or default_max_price)
            self.paused = bool(raw.get("paused", False))
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning("設定檔讀取失敗，使用預設：%s", e)

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"max_price": self.max_price, "paused": self.paused}, f)
        os.replace(tmp, self.path)


class Gate:
    """所有 Google 查詢的總閘門：兩次請求之間至少隨機間隔 delay_min~delay_max 秒。"""

    def __init__(
        self,
        search: Callable[[str], list[Fare]],
        cfg: Config,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ):
        self._search = search
        self.cfg = cfg
        self.sleep = sleep
        self.clock = clock
        self.rng = rng or random.Random()
        self.next_ok = 0.0
        self.lock = threading.Lock()

    def search(self, d: str) -> list[Fare]:
        with self.lock:
            wait = self.next_ok - self.clock()
            if wait > 0:
                self.sleep(wait)
            try:
                return self._search(d)
            finally:
                self.next_ok = self.clock() + self.rng.uniform(self.cfg.delay_min, self.cfg.delay_max)


# ---------------------------------------------------------------- Bot


class Bot:
    def __init__(
        self,
        cfg: Config,
        tg: Telegram,
        search: Callable[[str], list[Fare]],
        tp_fetch=None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        scan_interval: timedelta = timedelta(hours=3),
        gate_sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg = cfg
        self.tg = tg
        self.gate = Gate(search, cfg, sleep=gate_sleep)
        self.tp_fetch = tp_fetch
        self.now = now_fn
        self.scan_interval = scan_interval
        self.state = State.load(cfg.state_file)
        self.settings = Settings(
            os.path.join(os.path.dirname(cfg.state_file) or ".", "settings.json"), cfg.max_price
        )
        self.jobs: queue.Queue = queue.Queue()
        self.busy = ""  # 背景正在做的事，空字串 = 閒置
        self.awaiting = ""  # 等待使用者輸入："date" / "threshold"
        self.offset = 0
        self.next_scan = self._initial_next_scan()

    # ---------------- 時間 / 排程

    def _today(self) -> date:
        return self.now().astimezone(TAIPEI).date()

    def _initial_next_scan(self) -> datetime:
        now = self.now()
        if self.state.last_scan_at:
            nxt = datetime.fromisoformat(self.state.last_scan_at) + self.scan_interval
            return max(nxt, now + timedelta(seconds=30))
        return now + timedelta(seconds=30)

    def _schedule_next(self) -> None:
        jitter = timedelta(minutes=random.uniform(-15, 15))
        self.next_scan = self.now() + self.scan_interval + jitter

    # ---------------- 介面

    def keyboard(self) -> dict:
        return {
            "keyboard": [
                [BTN_CHEAPEST, BTN_MONTHS],
                [BTN_DATE, BTN_SCAN],
                [BTN_THRESHOLD, BTN_RESUME if self.settings.paused else BTN_PAUSE],
                [BTN_STATUS],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        }

    def reply(self, text: str, markup: dict | None = None) -> None:
        self.tg.send(text, reply_markup=markup or self.keyboard())

    def setup(self) -> None:
        self.tg.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})

    # ---------------- 主執行緒：處理 Telegram 更新

    def handle_update(self, update: dict) -> None:
        self.offset = max(self.offset, update.get("update_id", 0) + 1)
        if "callback_query" in update:
            cq = update["callback_query"]
            chat = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
            self.tg.call("answerCallbackQuery", {"callback_query_id": cq.get("id")})
            if chat == str(self.tg.chat_id):
                self.handle_callback(cq.get("data") or "")
            return
        msg = update.get("message") or {}
        chat = str((msg.get("chat") or {}).get("id", ""))
        if chat != str(self.tg.chat_id):
            log.warning("忽略非授權 chat %s 的訊息", chat)
            return
        self.handle_text((msg.get("text") or "").strip())

    def handle_text(self, text: str) -> None:
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        arg = arg.strip()

        actions: dict[str, Callable[[], None]] = {
            "/start": self.show_menu,
            "/menu": self.show_menu,
            BTN_CHEAPEST: self.show_cheapest,
            "/cheapest": self.show_cheapest,
            BTN_MONTHS: self.show_months,
            "/months": self.show_months,
            BTN_SCAN: self.request_scan,
            "/scan": self.request_scan,
            BTN_THRESHOLD: self.ask_threshold,
            "/threshold": self.ask_threshold,
            BTN_PAUSE: lambda: self.set_paused(True),
            "/pause": lambda: self.set_paused(True),
            BTN_RESUME: lambda: self.set_paused(False),
            "/resume": lambda: self.set_paused(False),
            BTN_STATUS: self.show_status,
            "/status": self.show_status,
        }
        if text in actions:
            self.awaiting = ""
            actions[text]()
            return
        if cmd in actions and not arg:
            self.awaiting = ""
            actions[cmd]()
            return
        if text == BTN_DATE or (cmd == "/date" and not arg):
            self.awaiting = "date"
            self.reply("請輸入出發日期，例如 <code>11/18</code> 或 <code>2026-11-18</code>")
            return
        if cmd == "/date":
            self.awaiting = ""
            self.request_date(arg)
            return
        if cmd == "/threshold" and arg:
            self.awaiting = ""
            self.set_threshold_text(arg)
            return

        if self.awaiting == "threshold":
            self.awaiting = ""
            self.set_threshold_text(text)
            return
        # 等待日期中，或直接打一個日期也能查
        if self.awaiting == "date" or parse_date(text, self._today()):
            self.awaiting = ""
            self.request_date(text)
            return
        self.reply("看不懂這個指令，請用下方按鈕 👇")

    def handle_callback(self, data: str) -> None:
        if data.startswith("thr:"):
            value = data[4:]
            if value == "custom":
                self.awaiting = "threshold"
                self.reply("請輸入新的門檻金額（TWD），例如 <code>3800</code>")
            else:
                self.set_threshold(int(value))
        elif data == "recheck":
            today = self._today().isoformat()
            dates: list[str] = []
            for f, _ in known_fares(self.state, today):
                if f.date not in dates:
                    dates.append(f.date)
                if len(dates) >= 5:
                    break
            if not dates:
                self.reply("還沒有資料可以重查")
                return
            self.enqueue(("recheck", dates), f"即時重查 {len(dates)} 個日期")
        elif data == "cheapest":
            self.show_cheapest()

    # ---------------- 各功能

    def show_menu(self) -> None:
        self.reply(
            f"✈️ <b>{self.cfg.origin} → {self.cfg.destination} 機票監控</b>\n"
            f"單程低於 NT${self.settings.max_price:,} 會自動通知你。\n"
            "用下方按鈕操作 👇"
        )

    def show_cheapest(self) -> None:
        now = self.now()
        items = known_fares(self.state, self._today().isoformat())[:5]
        if not items:
            self.reply("還沒有價格資料，第一輪掃描完成後再試，或按「🔍 立即掃描」")
            return
        lines = [f"🔥 <b>目前已知最便宜 5 張（{self.cfg.origin} → {self.cfg.destination} 單程）</b>", ""]
        for i, (f, checked) in enumerate(items, 1):
            lines += fare_lines(i, f, self.settings.max_price, checked, now)
            lines.append("")
        lines.append(f"已掌握 {len(self.state.dates)} 個日期的價格")
        self.reply(
            "\n".join(lines),
            {"inline_keyboard": [[{"text": "🔄 即時重查這幾天", "callback_data": "recheck"}]]},
        )

    def show_months(self) -> None:
        today = self._today().isoformat()
        months: dict[str, tuple[int, str, str]] = {}
        counts: dict[str, int] = {}
        for d, info in self.state.dates.items():
            if d < today:
                continue
            m = d[:7]
            counts[m] = counts.get(m, 0) + 1
            p = info.get("min_price")
            if p is not None and (m not in months or p < months[m][0]):
                months[m] = (p, d, info.get("airline", ""))
        if not counts:
            self.reply("還沒有價格資料")
            return
        lines = ["📆 <b>各月最低價</b>", ""]
        for m in sorted(counts):
            if m in months:
                p, d, airline = months[m]
                fire = "🔥" if p < self.settings.max_price else ""
                lines.append(f"<b>{m}</b>  NT${p:,}{fire}  {fmt_date(d)} {html.escape(airline)}")
            else:
                lines.append(f"<b>{m}</b>  無航班資料")
            lines.append(f"   已查 {counts[m]} 天")
        self.reply("\n".join(lines))

    def show_status(self) -> None:
        now = self.now()
        st = self.state
        window = self.cfg.days_ahead_max - self.cfg.days_ahead_min + 1
        lines = [
            "ℹ️ <b>系統狀態</b>",
            f"路線：{self.cfg.origin} → {self.cfg.destination}（{'直飛' if self.cfg.max_stops == 0 else f'最多轉 {self.cfg.max_stops} 次'}）",
            f"通知門檻：NT${self.settings.max_price:,}",
            f"通知：{'⏸ 已暫停' if self.settings.paused else '✅ 開啟'}",
            f"監控範圍：未來 {self.cfg.days_ahead_min}–{self.cfg.days_ahead_max} 天（已掌握 {len(st.dates)}/{window} 天）",
            f"上次掃描：{ago(st.last_scan_at, now) if st.last_scan_at else '尚未掃描'}",
            f"下次掃描：約 {max(0, int((self.next_scan - now).total_seconds() // 60))} 分鐘後",
            f"目前工作：{self.busy or '閒置'}",
            f"Travelpayouts：{'啟用' if self.tp_fetch else '未設定'}",
        ]
        if st.fail_streak:
            lines.append(f"⚠️ 連續 {st.fail_streak} 輪查詢失敗")
        self.reply("\n".join(lines))

    def ask_threshold(self) -> None:
        row = [{"text": f"{p:,}", "callback_data": f"thr:{p}"} for p in THRESHOLD_PRESETS]
        self.tg.send(
            f"💰 目前門檻：NT${self.settings.max_price:,}\n選一個新門檻，或自訂：",
            reply_markup={
                "inline_keyboard": [row[:3], row[3:], [{"text": "✏️ 自訂金額", "callback_data": "thr:custom"}]]
            },
        )

    def set_threshold_text(self, text: str) -> None:
        digits = re.sub(r"[,\s元NT$]", "", text, flags=re.I)
        if not digits.isdigit() or not (500 <= int(digits) <= 100000):
            self.reply("金額格式不對，請輸入 500 ~ 100000 之間的數字")
            return
        self.set_threshold(int(digits))

    def set_threshold(self, value: int) -> None:
        self.settings.max_price = value
        self.settings.save()
        below = [f for f, _ in known_fares(self.state, self._today().isoformat()) if f.price < value]
        msg = f"✅ 門檻已設為 NT${value:,}"
        if below:
            msg += f"\n目前已知有 {len(below)} 張低於這個價格，按「{BTN_CHEAPEST}」查看"
        self.reply(msg)

    def set_paused(self, paused: bool) -> None:
        self.settings.paused = paused
        self.settings.save()
        if paused:
            self.reply("⏸ 已暫停通知（背景仍會繼續掃描，按鈕查詢照常可用）")
        else:
            self.reply("▶️ 已恢復通知")

    def request_scan(self) -> None:
        last = self.state.last_scan_at
        if last and self.now() - datetime.fromisoformat(last) < SCAN_COOLDOWN:
            wait = SCAN_COOLDOWN - (self.now() - datetime.fromisoformat(last))
            self.reply(f"剛掃描過，為了避免被封鎖請 {int(wait.total_seconds() // 60) + 1} 分鐘後再試")
            return
        self.enqueue(("scan", True), "掃描")

    def request_date(self, text: str) -> None:
        today = self._today()
        d = parse_date(text, today)
        if d is None:
            self.reply("日期格式不對，請輸入例如 <code>11/18</code> 或 <code>2026-11-18</code>")
            return
        if not (today <= d <= today + timedelta(days=MAX_DAYS_AHEAD)):
            self.reply(f"只能查今天到 {MAX_DAYS_AHEAD} 天內的日期")
            return
        self.enqueue(("date", d.isoformat()), f"查詢 {d.isoformat()}")

    def enqueue(self, job: tuple, label: str) -> None:
        if self.busy:
            self.reply(f"⏳ 已排入佇列：{label}\n（目前正在{self.busy}，完成後馬上處理）")
        else:
            self.reply(f"⏳ {label}中，請稍候…")
        self.jobs.put(job)

    # ---------------- 背景執行緒：真正發出 Google 請求

    def process_job(self, job: tuple) -> None:
        kind = job[0]
        if kind == "scan":
            self.busy = "掃描"
            self.do_scan(manual=job[1])
        elif kind == "date":
            self.busy = f"查詢 {job[1]}"
            self.do_dates([job[1]], reply="date")
        elif kind == "recheck":
            self.busy = "重查"
            self.do_dates(job[1], reply="cheapest")
        self.busy = ""

    def _commit(self, new_state: State) -> None:
        new_state.save(self.cfg.state_file)
        self.state = new_state  # 整個換掉，主執行緒讀到的永遠是完整的一份

    def do_scan(self, manual: bool) -> None:
        now = self.now()
        cfg = dataclasses.replace(self.cfg, max_price=self.settings.max_price)
        tg = MutedTelegram() if self.settings.paused else self.tg
        new = copy.deepcopy(self.state)
        try:
            new = run(cfg, new, self.gate.search, tg, now, tp_fetch=self.tp_fetch, sleep=lambda _: None)
        except Exception:
            log.exception("掃描失敗")
            if manual:
                self.reply("❌ 掃描發生錯誤，請看 log")
            return
        finally:
            self._schedule_next()
        new.last_scan_at = now.isoformat()
        self._commit(new)
        if manual:
            checked = [d for d, v in new.dates.items() if v.get("checked_at") == now.isoformat()]
            prices = [(new.dates[d]["min_price"], d) for d in checked if new.dates[d].get("min_price")]
            msg = f"✅ 掃描完成：查了 {len(checked)} 個日期"
            if prices:
                p, d = min(prices)
                msg += f"\n本輪最低：{fmt_date(d)} NT${p:,}"
            elif not checked:
                msg += "\n⚠️ 全部查詢失敗，可能暫時被限流"
            self.reply(msg)

    def do_dates(self, dates: list[str], reply: str) -> None:
        now = self.now()
        new = copy.deepcopy(self.state)
        results: dict[str, list[Fare]] = {}
        failed = []
        for d in dates:
            try:
                fares = self.gate.search(d)
            except BlockedError as e:
                log.warning("%s 查詢失敗：%s", d, e)
                failed.append(d)
                if "HTTP 429" in str(e) or "HTTP 403" in str(e):
                    break
                continue
            results[d] = fares
            record_date(new, d, fares, now)
            # 使用者已經在回覆裡看到了，標記為已通知，避免下一輪再推一次
            for f in new_deals_for_date(new, d, fares, self.settings.max_price):
                new.notified[f.key] = f.price
        self._commit(new)

        if reply == "date":
            d = dates[0]
            if d in failed:
                self.reply("❌ 查詢失敗，可能暫時被限流，請稍後再試")
                return
            fares = results.get(d, [])
            if not fares:
                self.reply(f"📅 {fmt_date(d)} 沒有找到{'直飛' if self.cfg.max_stops == 0 else ''}航班")
                return
            lines = [f"📅 <b>{fmt_date(d)} {self.cfg.origin} → {self.cfg.destination}</b>", ""]
            for i, f in enumerate(fares[:5], 1):
                lines += fare_lines(i, f, self.settings.max_price)
            self.reply("\n".join(lines))
        else:
            if failed:
                self.reply(f"⚠️ 有 {len(failed)} 個日期查詢失敗，以下含部分舊資料")
            self.show_cheapest()

    def worker(self, stop: threading.Event) -> None:
        while not stop.is_set():
            timeout = max(1.0, (self.next_scan - self.now()).total_seconds())
            try:
                job = self.jobs.get(timeout=min(timeout, 60))
            except queue.Empty:
                if self.now() >= self.next_scan:
                    job = ("scan", False)
                else:
                    continue
            try:
                self.process_job(job)
            except Exception:
                log.exception("工作失敗：%s", job)
                self.busy = ""

    # ---------------- 進入點

    def run_forever(self) -> None:
        self.setup()
        stop = threading.Event()
        threading.Thread(target=self.worker, args=(stop,), daemon=True).start()
        log.info("Bot 啟動，下次掃描：%s", self.next_scan.astimezone(TAIPEI).strftime("%m-%d %H:%M"))
        backoff = 5
        try:
            while True:
                try:
                    updates = self.tg.get_updates(self.offset)
                    backoff = 5
                except Exception as e:
                    log.warning("getUpdates 失敗：%s（%d 秒後重試）", e, backoff)
                    time.sleep(backoff)
                    backoff = min(backoff * 2, 300)
                    continue
                for u in updates:
                    try:
                        self.handle_update(u)
                    except Exception:
                        log.exception("處理訊息失敗")
        finally:
            stop.set()
