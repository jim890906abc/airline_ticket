"""透過 Google Flights 查單一日期的單程票價。

Google Flights 會聚合虎航、樂桃、捷星、酷航等廉航與傳統航空的價格，
是覆蓋率最高、也最不容易被擋的單一來源（相較於直接打各家航空官網）。

降低被封鎖風險的做法：
- 整輪查詢共用同一個 client（保留 cookie，像同一個瀏覽器在連續查詢）
- 每個請求之間由呼叫端隨機等待（見 main.py）
- 回應異常（非 200、頁面結構不對）就拋出 BlockedError，讓呼叫端立刻停手
"""

from __future__ import annotations

import json

from fast_flights import FlightQuery, create_query
from fast_flights.exceptions import FlightsNotFound
from fast_flights.parser import parse_js
from primp import Client
from selectolax.lexbor import LexborHTMLParser

from .models import Fare

URL = "https://www.google.com/travel/flights"


class BlockedError(Exception):
    """疑似被限流 / 擋下，或 Google 頁面結構變了。"""


class GoogleFlightsClient:
    def __init__(self, origin: str, destination: str, currency: str, max_stops: int):
        self.origin = origin
        self.destination = destination
        self.currency = currency
        self.max_stops = max_stops
        self._client = Client(
            impersonate="chrome_145",
            impersonate_os="macos",
            referer=True,
            cookie_store=True,
            timeout=30,
        )

    def _query(self, date: str):
        return create_query(
            flights=[
                FlightQuery(
                    date=date,
                    from_airport=self.origin,
                    to_airport=self.destination,
                    max_stops=self.max_stops,
                )
            ],
            trip="one-way",
            seat="economy",
            language="zh-TW",
            currency=self.currency,
        )

    def search(self, date: str) -> list[Fare]:
        q = self._query(date)
        try:
            res = self._client.get(URL, params=q.params())
        except Exception as e:  # 連線錯誤也視為可能被擋，交由呼叫端退避
            raise BlockedError(f"request failed: {e}") from e

        if res.status_code != 200:
            raise BlockedError(f"HTTP {res.status_code}")
        return parse_fares(res.text, date=date, link=q.url())


def parse_fares(html: str, *, date: str, link: str) -> list[Fare]:
    script = LexborHTMLParser(html).css_first(r"script.ds\:1")
    if script is None:
        # 沒有資料區塊：通常是 consent 頁、驗證碼或 Google 改版
        raise BlockedError("flight data block not found in page")

    js = script.text()
    try:
        payload = json.loads(js.split("data:", 1)[1].rsplit(",", 1)[0])
    except (IndexError, ValueError) as e:
        if "errorHasStatus: true" in js:
            return []  # 該日期沒有航班
        raise BlockedError(f"unexpected page format: {e}") from e

    # fast-flights 的 parser 只讀 payload[3]（其他航班），
    # 最便宜的那班有時被放在 payload[2]（最佳航班），所以兩區都各自解析。
    flights: list = []
    names: dict[str, str] = {}
    parsed_any = False
    last_error: Exception | None = None
    for idx in (2, 3):
        try:
            section = payload[idx][0]
        except (IndexError, TypeError):
            continue
        if not isinstance(section, list) or not section:
            continue
        merged = list(payload) + [None] * max(0, 4 - len(payload))
        merged[3] = [section]
        try:
            result = parse_js("data:" + json.dumps(merged) + ",0")
        except FlightsNotFound:
            parsed_any = True
            continue
        except Exception as e:  # 某一區結構不同時略過，不影響另一區
            last_error = e
            continue
        parsed_any = True
        flights.extend(result)
        meta = getattr(result, "metadata", None)
        if meta is not None:
            names.update({a.code: a.name for a in meta.airlines})

    if not parsed_any and last_error is not None:
        raise BlockedError(f"failed to parse flights: {last_error}") from last_error

    fares: dict[str, Fare] = {}
    for f in flights:
        if not f.flights or not f.price:
            continue
        first, last = f.flights[0], f.flights[-1]
        airline = "、".join(names.get(a, a) for a in f.airlines) or "?"
        fare = Fare(
            date=date,
            price=int(f.price),
            airline=airline,
            depart_time="%02d:%02d" % first.departure.time,
            arrive_time="%02d:%02d" % last.arrival.time,
            stops=len(f.flights) - 1,
            source="google",
            link=link,
        )
        # 同一班機可能同時出現在「最佳」與「其他」，保留較低價
        if fare.key not in fares or fare.price < fares[fare.key].price:
            fares[fare.key] = fare
    return sorted(fares.values(), key=lambda x: x.price)
