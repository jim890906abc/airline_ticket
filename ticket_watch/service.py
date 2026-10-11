"""常駐服務的核心：共用狀態、Google 請求閘門、背景工作（定時掃描 / 網站即時查詢）。

執行緒模型：
- 網站（多執行緒）只「讀」self.state，並把查詢工作丟進佇列
- 背景 worker 是唯一會寫入 state 的執行緒；每查完一個日期就把整份 state 換成新的快照，
  網站讀到的永遠是完整一致的一份
- 所有 Google 請求都經過 Gate：兩次請求之間至少隨機間隔 delay_min~delay_max 秒
"""

from __future__ import annotations

import copy
import logging
import os
import queue
import random
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from .config import Config
from .google_flights import BlockedError
from .models import ROUTES, Fare, route_id, route_label
from .rules import MAX_DAYS_AHEAD, Settings
from .scanner import (
    Search,
    deliver,
    new_deals,
    scan_round,
    taipei_today,
    watch_targets,
)
from .state import State, split_key

log = logging.getLogger("ticket_watch.service")

SCAN_COOLDOWN = timedelta(minutes=20)
MAX_SEARCH_DATES = 93  # 單次即時查詢最多幾個日期（約 3 個月）
REPO_BRANCH = "claude/airline-ticket-notifier-5i70uz"
INSTALL_CMD = (
    "curl -fsSL https://raw.githubusercontent.com/jim890906abc/airline_ticket/"
    f"{REPO_BRANCH}/install.sh | TELEGRAM_BOT_TOKEN=你的token bash"
)


def update_cmd(install_dir: str) -> str:
    return (
        f"cd {install_dir} && git fetch -q origin {REPO_BRANCH} "
        "&& git reset -q --hard FETCH_HEAD && bash install.sh"
    )


class Gate:
    """所有 Google 查詢的總閘門。"""

    def __init__(
        self,
        search: Search,
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

    def search(self, origin: str, destination: str, d: str) -> list[Fare]:
        with self.lock:
            wait = self.next_ok - self.clock()
            if wait > 0:
                self.sleep(wait)
            try:
                return self._search(origin, destination, d)
            finally:
                self.next_ok = self.clock() + self.rng.uniform(self.cfg.delay_min, self.cfg.delay_max)


@dataclass
class Job:
    id: str
    kind: str  # "scan" / "search"
    label: str
    params: dict = field(default_factory=dict)
    status: str = "queued"  # queued / running / done / failed / cancelled
    total: int = 0
    done: int = 0
    to_fetch: int = 0  # 需要真的發請求的數量（其餘用快取）
    results: list[dict] = field(default_factory=list)
    message: str = ""
    created_at: str = ""
    finished_at: str = ""
    cancel: bool = False

    def to_dict(self, avg_delay: float) -> dict:
        fetched = sum(1 for r in self.results if not r.get("from_cache"))
        remaining = max(0, self.to_fetch - fetched)
        return {
            "id": self.id,
            "kind": self.kind,
            "label": self.label,
            "params": self.params,
            "status": self.status,
            "total": self.total,
            "done": self.done,
            "eta_seconds": int(remaining * avg_delay) if self.status in ("queued", "running") else 0,
            "results": self.results,
            "message": self.message,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class Service:
    def __init__(
        self,
        cfg: Config,
        notifier,
        search: Search,
        tp_fetch=None,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        gate_sleep: Callable[[float], None] = time.sleep,
        install_dir: str = "",
    ):
        self.cfg = cfg
        self.notifier = notifier
        self.gate = Gate(search, cfg, sleep=gate_sleep)
        self.tp_fetch = tp_fetch
        self.now = now_fn
        self.install_dir = install_dir or os.getcwd()
        self.state = State.load(cfg.state_file)
        self.settings = Settings(
            os.path.join(os.path.dirname(cfg.state_file) or ".", "settings.json"),
            cfg.max_price,
            cfg.dates_per_run,
        )
        self.jobs: dict[str, Job] = {}
        self.queue: queue.Queue[str] = queue.Queue()
        self.current: Job | None = None
        self.jobs_lock = threading.Lock()
        self.next_scan = self._initial_next_scan()
        self.tunnel = None  # main.run_server 會設定（ticket_watch.tunnel.Tunnel）

    def public_url(self) -> str:
        return self.tunnel.url if self.tunnel else ""

    # ---------------------------------------------------------------- 共用

    @property
    def avg_delay(self) -> float:
        return (self.cfg.delay_min + self.cfg.delay_max) / 2 + 2

    def today(self) -> date:
        return taipei_today(self.now())

    def out(self):
        """暫停通知時不發送，且回傳 False 讓班機不被標記，恢復後仍會通知。"""
        if self.settings.paused:
            return _Muted()
        return self.notifier

    def _initial_next_scan(self) -> datetime:
        now = self.now()
        if self.state.last_scan_at:
            interval = timedelta(hours=self.settings.scan_interval_hours)
            return max(datetime.fromisoformat(self.state.last_scan_at) + interval, now + timedelta(seconds=30))
        return now + timedelta(seconds=30)

    def _schedule_next(self) -> None:
        jitter = timedelta(minutes=random.uniform(-15, 15))
        self.next_scan = self.now() + timedelta(hours=self.settings.scan_interval_hours) + jitter

    def _commit(self, working: State) -> None:
        snap = copy.deepcopy(working)
        snap.save(self.cfg.state_file)
        self.state = snap

    # ---------------------------------------------------------------- 工作佇列

    def _add_job(self, kind: str, label: str, params: dict, enqueue: bool = True) -> Job:
        with self.jobs_lock:
            job = Job(
                id=secrets.token_hex(4),
                kind=kind,
                label=label,
                params=params,
                created_at=self.now().isoformat(),
            )
            self.jobs[job.id] = job
            # 只保留最近 30 筆
            for old in list(self.jobs)[:-30]:
                if self.jobs[old].status not in ("queued", "running"):
                    del self.jobs[old]
        if enqueue:
            self.queue.put(job.id)
        return job

    def job_dict(self, job_id: str) -> dict | None:
        job = self.jobs.get(job_id)
        return job.to_dict(self.avg_delay) if job else None

    def list_jobs(self) -> list[dict]:
        out = []
        for j in reversed(list(self.jobs.values())):
            d = j.to_dict(self.avg_delay)
            d.pop("results")
            out.append(d)
        return out

    def cancel_job(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        job.cancel = True
        if job.status == "queued":
            job.status = "cancelled"
        return True

    def request_scan(self) -> Job:
        for j in self.jobs.values():
            if j.kind == "scan" and j.status in ("queued", "running"):
                raise ValueError("已經有一輪掃描在排隊或執行中")
        last = self.state.last_scan_at
        if last and self.now() - datetime.fromisoformat(last) < SCAN_COOLDOWN:
            wait = SCAN_COOLDOWN - (self.now() - datetime.fromisoformat(last))
            raise ValueError(f"剛掃描過，為避免被封鎖請 {int(wait.total_seconds() // 60) + 1} 分鐘後再試")
        return self._add_job("scan", "手動掃描", {"manual": True})

    def request_search(self, legs: list[dict], max_age_hours: float = 3) -> Job:
        """即時查詢：legs = [{"origin","destination","date_from","date_to","weekdays"?}]。"""
        today = self.today()
        last_day = today + timedelta(days=MAX_DAYS_AHEAD)
        targets: list[tuple[str, str]] = []
        labels = []
        for leg in legs:
            origin, destination = str(leg.get("origin", "")).upper(), str(leg.get("destination", "")).upper()
            if (origin, destination) not in ROUTES:
                raise ValueError("不支援的航線")
            try:
                start = date.fromisoformat(leg["date_from"])
                end = date.fromisoformat(leg.get("date_to") or leg["date_from"])
            except (KeyError, ValueError):
                raise ValueError("日期格式錯誤")
            if start > end:
                raise ValueError("開始日期不能晚於結束日期")
            start, end = max(start, today), min(end, last_day)
            if start > end:
                raise ValueError(f"只能查今天到 {MAX_DAYS_AHEAD} 天內的日期")
            weekdays = {int(w) for w in leg.get("weekdays") or []}
            d = start
            while d <= end:
                if not weekdays or d.weekday() in weekdays:
                    t = (route_id(origin, destination), d.isoformat())
                    if t not in targets:
                        targets.append(t)
                d += timedelta(days=1)
            labels.append(f"{route_label(origin, destination)} {start.isoformat()}~{end.isoformat()}")
        if not targets:
            raise ValueError("區間內沒有符合條件的日期")
        if len(targets) > MAX_SEARCH_DATES:
            raise ValueError(f"一次最多查 {MAX_SEARCH_DATES} 個日期（目前 {len(targets)} 個），請縮小區間")
        max_age = max(0.0, float(max_age_hours))
        job = self._add_job(
            "search",
            "；".join(labels),
            {"targets": targets, "max_age_hours": max_age},
        )
        job.total = len(targets)
        job.to_fetch = sum(1 for t in targets if not self._fresh(t, max_age))
        return job

    def _fresh(self, target: tuple[str, str], max_age_hours: float) -> bool:
        info = self.state.info(*target)
        if not info or not info.get("checked_at") or max_age_hours <= 0:
            return False
        return self.now() - datetime.fromisoformat(info["checked_at"]) < timedelta(hours=max_age_hours)

    # ---------------------------------------------------------------- 背景 worker

    def process(self, job: Job) -> None:
        if job.cancel:
            job.status = "cancelled"
            return
        job.status = "running"
        self.current = job
        try:
            if job.kind == "scan":
                self._do_scan(job)
            else:
                self._do_search(job)
            if job.status == "running":
                job.status = "cancelled" if job.cancel else "done"
        except Exception as e:
            log.exception("工作失敗")
            job.status = "failed"
            job.message = str(e)
        finally:
            job.finished_at = self.now().isoformat()
            self.current = None

    def _do_scan(self, job: Job) -> None:
        working = copy.deepcopy(self.state)
        rules = self.settings.enabled_rules()

        def progress(i, n, target, fares):
            job.total, job.done, job.to_fetch = n, i, n
            job.results.append(_result(target, fares, working, from_cache=False))

        try:
            stats = scan_round(
                self.cfg,
                working,
                rules,
                self.settings.dates_per_run,
                self.gate.search,
                self.out(),
                self.now(),
                tp_fetch=self.tp_fetch,
                on_progress=progress,
                should_stop=lambda: job.cancel,
                commit=self._commit,
            )
        finally:
            self._schedule_next()
        self._commit(working)
        job.to_fetch = job.total = stats["planned"]
        if not stats["planned"]:
            job.message = "沒有啟用中的通知規則，未掃描"
        else:
            job.message = f"查了 {stats['checked']} 個日期" + (f"，{stats['failed']} 個失敗" if stats["failed"] else "")
            if stats["aborted"] and not job.cancel:
                job.message += "（疑似被限流，提前結束）"

    def _do_search(self, job: Job) -> None:
        working = copy.deepcopy(self.state)
        rules = self.settings.enabled_rules()
        today = self.today()
        max_age = job.params["max_age_hours"]
        deals: list[Fare] = []
        errors_in_row = 0
        for route, d in job.params["targets"]:
            if job.cancel:
                break
            target = (route, d)
            if self._fresh(target, max_age):
                job.results.append(_result(target, working.fares_for(route, d), working, from_cache=True))
                job.done += 1
                continue
            origin, _, destination = route.partition("-")
            now = self.now()
            try:
                fares = self.gate.search(origin, destination, d)
            except BlockedError as e:
                log.warning("%s %s 查詢失敗：%s", route, d, e)
                job.results.append(_result(target, None, working, from_cache=False, error=str(e)))
                job.done += 1
                errors_in_row += 1
                if errors_in_row >= self.cfg.max_consecutive_errors or "HTTP 429" in str(e) or "HTTP 403" in str(e):
                    job.message = "疑似被 Google 限流，已停止查詢，請稍後再試"
                    break
                continue
            errors_in_row = 0
            working.record(route, d, fares, now)
            deals.extend(new_deals(working, route, d, fares, rules, today))
            self._commit(working)
            job.results.append(_result(target, fares, working, from_cache=False))
            job.done += 1
        # 即時查詢查到符合通知規則的低價，一樣推播到 Telegram
        deliver(deals, self.out(), working)
        self._commit(working)

    def worker(self, stop: threading.Event) -> None:
        while not stop.is_set():
            timeout = max(1.0, min(60.0, (self.next_scan - self.now()).total_seconds()))
            try:
                job_id = self.queue.get(timeout=timeout)
                job = self.jobs.get(job_id)
            except queue.Empty:
                if self.now() < self.next_scan:
                    continue
                job = self._add_job("scan", "定時掃描", {"manual": False}, enqueue=False)
            if job is None or job.status == "cancelled":
                continue
            self.process(job)

    # ---------------------------------------------------------------- 給網站的資料

    def overview(self) -> dict:
        today = self.today()
        st = self.state
        last_day = (today + timedelta(days=MAX_DAYS_AHEAD)).isoformat()
        rules = self.settings.enabled_rules()

        def threshold(f: Fare) -> int | None:
            ps = [r.max_price for r in rules if r.covers(f.route, f.date, today)]
            return max(ps) if ps else None

        cheapest = {}
        for origin, destination in ROUTES:
            route = route_id(origin, destination)
            cheapest[route] = [
                {**f.to_dict(), "checked_at": checked, "threshold": threshold(f)}
                for f, checked in st.known_fares(route, today.isoformat(), last_day)[:10]
            ]
        counts = {route_id(o, d): 0 for o, d in ROUTES}
        for key in st.dates:
            r, _ = split_key(key)
            if r in counts:
                counts[r] += 1
        targets = watch_targets(rules, today)
        covered = sum(1 for t in targets if st.info(*t))
        return {
            "now": self.now().isoformat(),
            "routes": [
                {"id": route_id(o, d), "origin": o, "destination": d, "label": route_label(o, d)} for o, d in ROUTES
            ],
            "cheapest": cheapest,
            "known_dates": counts,
            "watch": {"targets": len(targets), "covered": covered},
            "last_scan_at": st.last_scan_at,
            "next_scan_at": self.next_scan.isoformat(),
            "fail_streak": st.fail_streak,
            "paused": self.settings.paused,
            "public_url": self.public_url(),
            "current_job": self.current.to_dict(self.avg_delay) if self.current else None,
            "queued_jobs": sum(1 for j in self.jobs.values() if j.status == "queued"),
        }

    def calendar(self, route: str) -> dict:
        today = self.today()
        rules = self.settings.enabled_rules()
        out = {}
        d, end = today, today + timedelta(days=MAX_DAYS_AHEAD)
        while d <= end:
            ds = d.isoformat()
            info = self.state.info(route, ds)
            thresholds = [r.max_price for r in rules if r.covers(route, ds, today)]
            if info or thresholds:
                out[ds] = {
                    "min_price": info.get("min_price") if info else None,
                    "airline": info.get("airline", "") if info else "",
                    "checked_at": info.get("checked_at", "") if info else "",
                    "threshold": max(thresholds) if thresholds else None,
                }
            d += timedelta(days=1)
        return {"route": route, "today": today.isoformat(), "last_day": end.isoformat(), "dates": out}

    def date_detail(self, route: str, d: str) -> dict:
        info = self.state.info(route, d) or {}
        return {
            "route": route,
            "date": d,
            "checked_at": info.get("checked_at", ""),
            "fares": [f.to_dict() for f in self.state.fares_for(route, d)],
        }

    def roundtrip(
        self,
        outbound: str,
        depart_from: str,
        depart_to: str,
        min_nights: int,
        max_nights: int,
        max_total: int | None = None,
        limit: int = 30,
    ) -> dict:
        origin, _, destination = outbound.partition("-")
        if (origin, destination) not in ROUTES:
            raise ValueError("不支援的航線")
        inbound = route_id(destination, origin)
        start, end = date.fromisoformat(depart_from), date.fromisoformat(depart_to)
        if start > end:
            raise ValueError("出發區間錯誤")
        if not (0 <= min_nights <= max_nights <= 60):
            raise ValueError("天數需介於 0 ~ 60，且最少 ≤ 最多")
        st = self.state
        combos = []
        missing_out, missing_back = set(), set()
        d = start
        while d <= end:
            out_fares = st.fares_for(outbound, d.isoformat())
            if not st.info(outbound, d.isoformat()):
                missing_out.add(d.isoformat())
            for n in range(min_nights, max_nights + 1):
                back = d + timedelta(days=n)
                back_fares = st.fares_for(inbound, back.isoformat())
                if not st.info(inbound, back.isoformat()):
                    missing_back.add(back.isoformat())
                if out_fares and back_fares:
                    total = out_fares[0].price + back_fares[0].price
                    if max_total is None or total <= max_total:
                        combos.append(
                            {
                                "total": total,
                                "nights": n,
                                "outbound": {**out_fares[0].to_dict(), "checked_at": st.info(outbound, d.isoformat())["checked_at"]},
                                "inbound": {**back_fares[0].to_dict(), "checked_at": st.info(inbound, back.isoformat())["checked_at"]},
                            }
                        )
            d += timedelta(days=1)
        combos.sort(key=lambda c: (c["total"], c["outbound"]["date"]))
        return {
            "combos": combos[:limit],
            "missing_outbound": len(missing_out),
            "missing_inbound": len(missing_back),
            "inbound_range": [(start + timedelta(days=min_nights)).isoformat(), (end + timedelta(days=max_nights)).isoformat()],
        }

    def system(self) -> dict:
        return {
            "settings": {
                "paused": self.settings.paused,
                "dates_per_run": self.settings.dates_per_run,
                "scan_interval_hours": self.settings.scan_interval_hours,
            },
            "config": {
                "max_stops": self.cfg.max_stops,
                "delay": [self.cfg.delay_min, self.cfg.delay_max],
                "travelpayouts": bool(self.tp_fetch),
                "currency": self.cfg.currency,
            },
            "version": _git_version(self.install_dir),
            "tunnel": self.tunnel.status() if self.tunnel else {"mode": "off", "url": "", "connected": False, "restarts": 0},
            "install_cmd": INSTALL_CMD,
            "update_cmd": update_cmd(_tilde(self.install_dir)),
            "install_dir": self.install_dir,
        }

    def start(self) -> threading.Event:
        stop = threading.Event()
        threading.Thread(target=self.worker, args=(stop,), daemon=True, name="worker").start()
        return stop


class _Muted:
    def send(self, text: str, reply_markup: dict | None = None) -> bool:
        log.info("通知已暫停，略過：%s", text.splitlines()[0] if text else "")
        return False


def _result(target, fares, state: State, from_cache: bool, error: str = "") -> dict:
    route, d = target
    info = state.info(route, d) or {}
    return {
        "route": route,
        "date": d,
        "from_cache": from_cache,
        "error": error,
        "checked_at": info.get("checked_at", ""),
        "fares": [f.to_dict() for f in (fares or [])][:5],
    }


def _git_version(path: str) -> str:
    try:
        return subprocess.run(
            ["git", "-C", path, "log", "-1", "--format=%h %cd %s", "--date=format:%Y-%m-%d %H:%M"],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except Exception:
        return ""


def _tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path
