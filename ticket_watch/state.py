"""跨次執行的狀態：每個日期上次查詢時間/最低價、已通知過的班機、失敗次數。"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field

log = logging.getLogger(__name__)


@dataclass
class State:
    # date -> {"checked_at": ISO 時間, "min_price": int | None, "airline": str}
    dates: dict[str, dict] = field(default_factory=dict)
    # Fare.key -> 已通知的價格
    notified: dict[str, int] = field(default_factory=dict)
    fail_streak: int = 0
    block_warned: bool = False
    last_summary: str = ""

    @classmethod
    def load(cls, path: str) -> "State":
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
            return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__})
        except FileNotFoundError:
            return cls()
        except Exception as e:
            log.warning("狀態檔損毀，重新開始：%s", e)
            return cls()

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, path)

    def prune(self, today_iso: str) -> None:
        """移除已經過去的日期。"""
        self.dates = {d: v for d, v in self.dates.items() if d >= today_iso}
        self.notified = {k: v for k, v in self.notified.items() if k[:10] >= today_iso}
