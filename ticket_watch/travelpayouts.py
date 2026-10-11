"""（選用）Travelpayouts / Aviasales Data API。

官方免費 API，一個請求就能拿到「整個月每一天」的最低價快取，
完全不會有被封鎖的問題。缺點是價格來自其他使用者近期的搜尋快取，
可能過時或漏掉某些日期，所以這裡只拿來「找出可能便宜的日期」，
再交給 Google Flights 驗證即時價格。

申請 token：https://www.travelpayouts.com/ → Profile → API token
"""

from __future__ import annotations

import logging
from datetime import date

import requests

from .models import Fare

log = logging.getLogger(__name__)

API = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"


def months_between(start: date, end: date) -> list[str]:
    months = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        months.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return months


def fetch_cheapest(
    token: str,
    origin: str,
    destination: str,
    currency: str,
    start: date,
    end: date,
    direct_only: bool,
    session: requests.Session | None = None,
) -> list[Fare]:
    http = session or requests.Session()
    fares: list[Fare] = []
    for month in months_between(start, end):
        params = {
            "origin": origin,
            "destination": destination,
            "departure_at": month,
            "one_way": "true",
            "direct": "true" if direct_only else "false",
            "currency": currency.lower(),
            "sorting": "price",
            "limit": 1000,
            "token": token,
        }
        try:
            res = http.get(API, params=params, timeout=30)
            res.raise_for_status()
            body = res.json()
        except Exception as e:
            log.warning("Travelpayouts %s 查詢失敗：%s", month, e)
            continue
        if not body.get("success", False):
            log.warning("Travelpayouts %s 回傳錯誤：%s", month, body.get("error"))
            continue
        if str(body.get("currency", currency)).upper() != currency.upper():
            log.warning("Travelpayouts 回傳幣別 %s 與設定不同，略過", body.get("currency"))
            continue

        for item in body.get("data") or []:
            try:
                dep = str(item["departure_at"])
                d = dep[:10]
                if not (start.isoformat() <= d <= end.isoformat()):
                    continue
                link = item.get("link") or ""
                fares.append(
                    Fare(
                        date=d,
                        price=int(item["price"]),
                        airline=str(item.get("airline") or "?"),
                        depart_time=dep[11:16] if len(dep) >= 16 else "",
                        arrive_time="",
                        stops=int(item.get("transfers") or 0),
                        source="travelpayouts",
                        link=f"https://www.aviasales.com{link}" if link.startswith("/") else link,
                        origin=origin,
                        destination=destination,
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
    return fares
