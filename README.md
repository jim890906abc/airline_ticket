# 桃園 → 大阪 低價機票 Telegram Bot

監控 **桃園機場 (TPE) → 大阪關西機場 (KIX)** 單程機票：出現 **低於 NT$3,500（含稅）** 的班機就發 Telegram 通知，
也可以在 bot 裡用按鈕隨時查詢最便宜的機票。

> 大阪有兩個機場，但伊丹 (ITM) 只飛日本國內線，從桃園出發的國際線都降關西 (KIX)，所以只查 KIX。

## 安裝（一行指令）

Mac / Linux / WSL，需要 Python 3.10+、git、cron。先在 Telegram 對 bot 按 **Start**，然後執行：

```bash
curl -fsSL https://raw.githubusercontent.com/jim890906abc/airline_ticket/claude/airline-ticket-notifier-5i70uz/install.sh | TELEGRAM_BOT_TOKEN=你的token bash
```

會自動：下載程式到 `~/airline_ticket` → 安裝套件 → 找出 chat id → 發測試訊息 → 設 cron 看門狗 → 啟動 bot。

## 更新（一行指令）

```bash
cd ~/airline_ticket && git fetch -q origin claude/airline-ticket-notifier-5i70uz && git reset -q --hard FETCH_HEAD && bash install.sh
```

直接對齊 GitHub 上的最新版本（不 merge，所以不會有衝突），沿用原本的 token / chat id / 價格紀錄 / 門檻設定，並自動重啟 bot。

## Bot 按鈕

傳 `/start` 或 `/menu` 會出現常駐按鈕：

| 按鈕 | 功能 |
| --- | --- |
| 🔥 最便宜5張 | 列出目前已知最便宜的 5 張機票（日期、價格、航空、時間、連結），下方有「🔄 即時重查這幾天」 |
| 📆 各月最低價 | 每個月最便宜的一天，看哪個月最划算 |
| 📅 查指定日期 | 輸入 `11/18` 或 `2026-11-18`，即時查那一天的所有航班（也可以直接打日期） |
| 🔍 立即掃描 | 馬上掃一輪（20 分鐘內只能按一次，避免被封） |
| 💰 設定門檻 | 3000 / 3500 / 4000 / 4500 / 5000 或自訂金額 |
| ⏸ 暫停通知 / ▶️ 恢復通知 | 暫停時仍會在背景掃描，恢復後還有效的低價會補通知 |
| ℹ️ 狀態 | 門檻、掃描進度、上次/下次掃描時間、是否被限流 |

只會回應你自己的 chat id，其他人傳訊息給 bot 不會有反應。

## 怎麼抓才有效又不會被 ban

| 做法 | 說明 |
| --- | --- |
| **用 Google Flights 當主要來源** | 已聚合虎航、樂桃、捷星、酷航、華航、長榮等所有航空，一個請求涵蓋全部。直接打各家廉航官網反而容易觸發 Akamai / Cloudflare 防爬。 |
| **少量、分散查詢** | 每 3 小時（±15 分鐘隨機）掃一次，每次 12 個日期，日期順序打亂。一天約 96 個請求，約 30 小時輪完未來 120 天。 |
| **全域請求閘門** | 不管是定時掃描還是按按鈕查詢，所有 Google 請求都排隊經過同一個閘門，間隔隨機 8–20 秒，狂按按鈕也不會爆量。 |
| **聰明挑日期** | 沒查過、最久沒查的日期優先；最近價格接近門檻（< 門檻 × 1.3）的日期更頻繁重查。 |
| **異常立刻停手** | HTTP 429/403 立刻停止本輪；連續 2 個日期失敗也停止。連續 3 輪都失敗會發一次警告，恢復時再通知。 |
| **（選用）Travelpayouts 官方 API** | 一個請求拿到整個月每天的最低價快取，零封鎖風險。用來找出可能便宜的日期，優先交給 Google 驗證。 |
| **不重複通知** | 同一班機只通知一次；更便宜、或漲回去又降下來才會再通知。 |

每天晚上 9 點後（台北時間）會發一則每日摘要，順便確認系統還活著。

## 常用指令

```bash
tail -f ~/airline_ticket/bot.log            # 看紀錄
~/airline_ticket/run.sh --date 2026-11-18   # 命令列查單日（不通知）
~/airline_ticket/run.sh --test-telegram     # 發測試訊息
```

移除：

```bash
crontab -l | grep -vF ~/airline_ticket/run.sh | crontab - ; kill $(cat ~/airline_ticket/state/bot.lock) ; rm -rf ~/airline_ticket
```

## 設定

安裝時可用環境變數帶入，或之後改 `~/airline_ticket/.env` 再跑一次更新指令：

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | BotFather 給的 token（必填） |
| `TELEGRAM_CHAT_ID` | 自動偵測 | 你的 chat id |
| `TRAVELPAYOUTS_TOKEN` | — | 選填，[travelpayouts.com](https://www.travelpayouts.com/) Profile → API token |
| `MAX_PRICE` | 3500 | 初始門檻（之後以 bot 裡設定的為準） |
| `MAX_STOPS` | 0 | 0 = 只看直飛 |
| `DAYS_AHEAD_MAX` | 120 | 監控未來幾天內出發的航班 |
| `DATES_PER_RUN` | 12 | 每輪掃描幾個日期 |

更多參數見 `ticket_watch/config.py`。

⚠️ 不要把 token commit 進 repo。token 若曾外流，到 BotFather 用 `/revoke` 換新的，再用 `TELEGRAM_BOT_TOKEN=新token` 跑一次安裝指令。

## 不想開電腦？用 GitHub Actions

`.github/workflows/check-fares.yml` 可以在 GitHub 上定時掃描並通知（**沒有按鈕功能**，因為 Actions 無法常駐）。
到 repo Settings → Secrets 設 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`，再把 workflow 裡的 `schedule` 取消註解即可。
不要和家裡的 bot 同時開，會重複查詢。

## 注意事項

- 價格為 Google Flights 顯示的含稅票價，**不含托運行李、選位**等加購。
- 這是用非官方方式讀取 Google Flights 網頁，Google 改版時可能失效，屆時會收到 Telegram 警告。

## 開發

```bash
pip install -r requirements.txt pytest
python -m pytest
```
