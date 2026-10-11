from __future__ import annotations

from dataclasses import dataclass

# 支援的航線（桃園 ⇄ 大阪關西）
ROUTES = [("TPE", "KIX"), ("KIX", "TPE")]
AIRPORT_NAMES = {"TPE": "桃園", "KIX": "大阪關西"}


def route_id(origin: str, destination: str) -> str:
    return f"{origin}-{destination}"


def route_label(origin: str, destination: str) -> str:
    return f"{AIRPORT_NAMES.get(origin, origin)} → {AIRPORT_NAMES.get(destination, destination)}"


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
    origin: str = "TPE"
    destination: str = "KIX"

    @property
    def route(self) -> str:
        return route_id(self.origin, self.destination)

    @property
    def key(self) -> str:
        """同一班機的識別鍵，用來避免重複通知。"""
        return f"{self.route}|{self.date}|{self.airline}|{self.depart_time}"

    def to_dict(self) -> dict:
        return {
            "origin": self.origin,
            "destination": self.destination,
            "date": self.date,
            "price": self.price,
            "airline": self.airline,
            "depart_time": self.depart_time,
            "arrive_time": self.arrive_time,
            "stops": self.stops,
            "source": self.source,
            "link": self.link,
        }
