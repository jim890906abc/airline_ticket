"""所有設定都從環境變數讀取，方便在 GitHub Actions / 本機 / Docker 共用。"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


@dataclass
class Config:
    origin: str = "TPE"  # 桃園機場
    destination: str = "KIX"  # 關西機場（大阪唯一有國際線的機場）
    max_price: int = 3500  # 單程低於此價格（TWD，含稅）就通知
    currency: str = "TWD"
    max_stops: int = 0  # 0 = 只看直飛
    days_ahead_min: int = 1
    days_ahead_max: int = 120
    dates_per_run: int = 12  # 每次執行最多查幾個日期（控制請求量）
    delay_min: float = 8.0  # 每個請求之間隨機等待秒數
    delay_max: float = 20.0
    max_consecutive_errors: int = 2  # 連續失敗幾次就中止本輪（疑似被擋）
    hot_margin: float = 1.3  # 最近價格 < max_price * hot_margin 的日期會更常被重查
    summary_hour: int = 21  # 台北時間幾點後送每日摘要（-1 = 關閉）
    block_alert_after: int = 3  # 連續幾輪全部失敗就發 Telegram 警告
    telegram_token: str = ""
    telegram_chat_id: str = ""
    travelpayouts_token: str = ""
    state_file: str = "state/state.json"
    dry_run: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            origin=os.environ.get("ORIGIN", "TPE").strip().upper() or "TPE",
            destination=os.environ.get("DESTINATION", "KIX").strip().upper() or "KIX",
            max_price=_int("MAX_PRICE", 3500),
            currency=os.environ.get("CURRENCY", "TWD").strip().upper() or "TWD",
            max_stops=_int("MAX_STOPS", 0),
            days_ahead_min=_int("DAYS_AHEAD_MIN", 1),
            days_ahead_max=_int("DAYS_AHEAD_MAX", 120),
            dates_per_run=_int("DATES_PER_RUN", 12),
            delay_min=_float("DELAY_MIN", 8.0),
            delay_max=_float("DELAY_MAX", 20.0),
            max_consecutive_errors=_int("MAX_CONSECUTIVE_ERRORS", 2),
            hot_margin=_float("HOT_MARGIN", 1.3),
            summary_hour=_int("SUMMARY_HOUR", 21),
            block_alert_after=_int("BLOCK_ALERT_AFTER", 3),
            telegram_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", "").strip(),
            travelpayouts_token=os.environ.get("TRAVELPAYOUTS_TOKEN", "").strip(),
            state_file=os.environ.get("STATE_FILE", "state/state.json").strip()
            or "state/state.json",
            dry_run=_bool("DRY_RUN", False),
        )
