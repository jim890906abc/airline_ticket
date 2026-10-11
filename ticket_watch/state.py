"""跨次執行的狀態：每條航線每個日期查到的價格、已通知過的班機、失敗次數。"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .models import Fare

log = logging.getLogger(__name__)

LEGACY_ROUTE = "TPE-KIX"  # 舊版只有這條航線，key 沒帶航線


def date_key(route: str, d: str) -> str:
    return f"{route}|{d}"


def split_key(key: str) -> tuple[str, str]:
    route, _, d = key.partition("|")
    return route, d


@dataclass
class State:
    # "TPE-KIX|2026-11-18" -> {"checked_at", "min_price", "airline", "link", "fares": [...]}
    dates: dict[str, dict] = field(default_factory=dict)
    # Fare.key -> 已通知的價格
    notified: dict[str, int] = field(default_factory=dict)
    fail_streak: int = 0
    block_warned: bool = False
    last_summary: str = ""
    last_scan_at: str = ""

    @classmethod
    def load(cls, path: str) -> "State":
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            st = cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        except FileNotFoundError:
            return cls()
        except Exception as e:
            log.warning("狀態檔損毀，重新開始：%s", e)
            return cls()
        st._migrate()
        return st

    def _migrate(self) -> None:
        if any("|" not in k for k in self.dates):
            self.dates = {(k if "|" in k else date_key(LEGACY_ROUTE, k)): v for k, v in self.dates.items()}
        self.notified = {
            (k if k[:1].isalpha() else f"{LEGACY_ROUTE}|{k}"): v for k, v in self.notified.items()
        }

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)

    def prune(self, today_iso: str) -> None:
        """移除已經過去的日期。"""
        self.dates = {k: v for k, v in self.dates.items() if split_key(k)[1] >= today_iso}
        self.notified = {
            k: v for k, v in self.notified.items() if k.split("|")[1] >= today_iso
        }

    # ---------------- 價格紀錄

    def record(self, route: str, d: str, fares: list[Fare], now: datetime, keep: int = 5) -> None:
        fares = sorted(fares, key=lambda f: f.price)
        cheapest = fares[0] if fares else None
        self.dates[date_key(route, d)] = {
            "checked_at": now.isoformat(),
            "min_price": cheapest.price if cheapest else None,
            "airline": cheapest.airline if cheapest else "",
            "link": cheapest.link if cheapest else "",
            "fares": [
                {"p": f.price, "a": f.airline, "dt": f.depart_time, "at": f.arrive_time, "s": f.stops}
                for f in fares[:keep]
            ],
        }

    def info(self, route: str, d: str) -> dict | None:
        return self.dates.get(date_key(route, d))

    def fares_for(self, route: str, d: str) -> list[Fare]:
        info = self.info(route, d)
        if not info:
            return []
        origin, _, destination = route.partition("-")
        return [
            Fare(d, x["p"], x["a"], x["dt"], x["at"], x["s"], "google", info.get("link", ""), origin, destination)
            for x in info.get("fares") or []
        ]

    def known_fares(self, route: str, date_from: str, date_to: str) -> list[tuple[Fare, str]]:
        """某航線、某日期區間內所有已知班機（含查詢時間），依價格排序。"""
        out = []
        for key, info in self.dates.items():
            r, d = split_key(key)
            if r != route or not (date_from <= d <= date_to):
                continue
            for f in self.fares_for(r, d):
                out.append((f, info.get("checked_at", "")))
        out.sort(key=lambda t: (t[0].price, t[0].date, t[0].depart_time))
        return out
