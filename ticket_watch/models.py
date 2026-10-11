from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Fare:
    date: str  # YYYY-MM-DD（出發日）
    price: int  # 單程總價（含稅）
    airline: str
    depart_time: str  # HH:MM，未知時為空字串
    arrive_time: str
    stops: int
    source: str  # "google" / "travelpayouts"
    link: str

    @property
    def key(self) -> str:
        """同一班機的識別鍵，用來避免重複通知。"""
        return f"{self.date}|{self.airline}|{self.depart_time}"
