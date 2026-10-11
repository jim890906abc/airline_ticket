"""網站上設定的 Telegram 通知規則與系統設定（存在 state/settings.json）。"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta

from .models import ROUTES, Fare, route_id

log = logging.getLogger(__name__)

MAX_DAYS_AHEAD = 330  # Google Flights 大約只開放到 330 天後


@dataclass
class Rule:
    """一條通知規則：某航線、某出發日期區間、低於某價格就通知。"""

    id: str = ""
    name: str = ""
    origin: str = "TPE"
    destination: str = "KIX"
    date_from: str = ""  # 空白 = 從明天開始
    date_to: str = ""  # 空白 = 滾動視窗（今天 + rolling_days）
    rolling_days: int = 120
    max_price: int = 3500
    weekdays: list[int] = field(default_factory=list)  # 0=週一…6=週日；空 = 不限
    enabled: bool = True

    @property
    def route(self) -> str:
        return route_id(self.origin, self.destination)

    def window(self, today: date) -> tuple[date, date] | None:
        """實際的出發日期區間（已裁切到今天 ~ 330 天內）；區間已過期回傳 None。"""
        start = date.fromisoformat(self.date_from) if self.date_from else today + timedelta(days=1)
        end = (
            date.fromisoformat(self.date_to)
            if self.date_to
            else today + timedelta(days=max(1, self.rolling_days))
        )
        start = max(start, today)
        end = min(end, today + timedelta(days=MAX_DAYS_AHEAD))
        return (start, end) if start <= end else None

    def covers(self, route: str, d: str, today: date) -> bool:
        if route != self.route:
            return False
        w = self.window(today)
        if not w or not (w[0].isoformat() <= d <= w[1].isoformat()):
            return False
        return not self.weekdays or date.fromisoformat(d).weekday() in self.weekdays

    def matches(self, fare: Fare, today: date) -> bool:
        return fare.price < self.max_price and self.covers(fare.route, fare.date, today)

    def validate(self) -> None:
        if (self.origin, self.destination) not in ROUTES:
            raise ValueError("不支援的航線")
        for d in (self.date_from, self.date_to):
            if d:
                date.fromisoformat(d)
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("開始日期不能晚於結束日期")
        if not (500 <= self.max_price <= 200000):
            raise ValueError("金額需介於 500 ~ 200000")
        if not (1 <= self.rolling_days <= MAX_DAYS_AHEAD):
            raise ValueError(f"滾動天數需介於 1 ~ {MAX_DAYS_AHEAD}")
        if any(w not in range(7) for w in self.weekdays):
            raise ValueError("星期格式錯誤")
        self.name = self.name.strip()[:40]

    @classmethod
    def from_dict(cls, raw: dict) -> "Rule":
        fields = cls.__dataclass_fields__
        r = cls(**{k: v for k, v in raw.items() if k in fields})
        r.max_price = int(r.max_price)
        r.rolling_days = int(r.rolling_days)
        r.weekdays = sorted({int(w) for w in r.weekdays})
        r.enabled = bool(r.enabled)
        r.origin, r.destination = r.origin.upper(), r.destination.upper()
        r.date_from, r.date_to = (r.date_from or "").strip(), (r.date_to or "").strip()
        return r


class Settings:
    """通知規則 + 可在網站上調整的系統設定。讀寫都加鎖，網站與背景執行緒共用。"""

    def __init__(self, path: str, default_max_price: int = 3500, default_dates_per_run: int = 12):
        self.path = path
        self.lock = threading.RLock()
        self.rules: list[Rule] = []
        self.paused = False
        self.dates_per_run = default_dates_per_run
        self.scan_interval_hours = 3.0
        self.keyboard_removed = False  # 舊版 Telegram 按鈕是否已清除
        exists = os.path.exists(path)
        if exists:
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                self.paused = bool(raw.get("paused", False))
                self.dates_per_run = int(raw.get("dates_per_run", default_dates_per_run))
                self.scan_interval_hours = float(raw.get("scan_interval_hours", 3.0))
                self.keyboard_removed = bool(raw.get("keyboard_removed", False))
                if "rules" in raw:
                    self.rules = [Rule.from_dict(r) for r in raw["rules"]]
                else:  # 舊版 bot 的設定：只有一個門檻
                    self.rules = [self._default_rule(int(raw.get("max_price") or default_max_price))]
            except Exception as e:
                log.warning("設定檔讀取失敗，使用預設：%s", e)
                exists = False
        if not exists:
            self.rules = [self._default_rule(default_max_price)]

    @staticmethod
    def _default_rule(max_price: int) -> Rule:
        return Rule(id=secrets.token_hex(4), name="桃園→大阪 預設", max_price=max_price)

    def save(self) -> None:
        with self.lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)

    def to_dict(self) -> dict:
        with self.lock:
            return {
                "paused": self.paused,
                "dates_per_run": self.dates_per_run,
                "scan_interval_hours": self.scan_interval_hours,
                "keyboard_removed": self.keyboard_removed,
                "rules": [asdict(r) for r in self.rules],
            }

    def enabled_rules(self) -> list[Rule]:
        with self.lock:
            return [Rule.from_dict(asdict(r)) for r in self.rules if r.enabled]

    def upsert_rule(self, raw: dict) -> Rule:
        rule = Rule.from_dict(raw)
        rule.validate()
        with self.lock:
            if rule.id and any(r.id == rule.id for r in self.rules):
                self.rules = [rule if r.id == rule.id else r for r in self.rules]
            else:
                if len(self.rules) >= 30:
                    raise ValueError("最多 30 條規則")
                rule.id = secrets.token_hex(4)
                self.rules.append(rule)
            self.save()
        return rule

    def delete_rule(self, rule_id: str) -> bool:
        with self.lock:
            before = len(self.rules)
            self.rules = [r for r in self.rules if r.id != rule_id]
            self.save()
            return len(self.rules) != before

    def update_system(self, raw: dict) -> None:
        with self.lock:
            if "paused" in raw:
                self.paused = bool(raw["paused"])
            if "dates_per_run" in raw:
                v = int(raw["dates_per_run"])
                if not 1 <= v <= 40:
                    raise ValueError("每輪日期數需介於 1 ~ 40")
                self.dates_per_run = v
            if "scan_interval_hours" in raw:
                v = float(raw["scan_interval_hours"])
                if not 1 <= v <= 24:
                    raise ValueError("掃描間隔需介於 1 ~ 24 小時")
                self.scan_interval_hours = v
            self.save()
