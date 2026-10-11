# 桃園 → 大阪 低價機票 Telegram 通知

監控 **桃園機場 (TPE) → 大阪關西機場 (KIX)** 單程機票，只要出現 **低於 NT$3,500（含稅）** 的班機就發 Telegram 訊息給你。
跑在 GitHub Actions 上，不用自己開電腦。

> 大阪有兩個機場，但伊丹 (ITM) 只飛日本國內線，從桃園出發的國際線都降關西 (KIX)，所以只查 KIX。

## 怎麼抓才有效又不會被 ban

| 做法 | 說明 |
| --- | --- |
| **用 Google Flights 當主要來源** | 它已經聚合了虎航、樂桃、捷星、酷航、華航、長榮等所有航空的價格，一個請求就涵蓋所有航空。直接打各家廉航官網反而容易觸發 Akamai / Cloudflare 防爬。 |
| **少量、分散查詢** | 每 3 小時執行一次，每次只查 12 個日期，請求間隔隨機 8–20 秒，日期順序也打亂。一天約 96 個請求，大概 30 小時就把未來 120 天全部輪過一遍。 |
| **聰明挑日期** | 從沒查過、最久沒查的日期優先；最近價格接近門檻（< 3500 × 1.3）的日期會被更頻繁重查。 |
| **異常立刻停手** | 遇到 HTTP 429/403 立刻停止本輪；連續 2 個日期失敗也停止。連續 3 輪都查不到會發一次 Telegram 警告，恢復時再通知。 |
| **（選用）Travelpayouts 官方 API** | Aviasales 的免費官方 API，**一個請求就拿到整個月每天的最低價快取**，完全沒有被封的風險。拿來找出「可能便宜」的日期，優先交給 Google Flights 驗證即時價格；若 Google 那輪剛好查不到，就先用快取價格通知（會標註需自行確認）。 |
| **不重複通知** | 同一班機只通知一次；之後更便宜、或漲回去又再降下來才會再通知。 |

另外每天晚上 9 點後（台北時間）會發一則每日摘要，列出目前已知最便宜的 5 個日期，順便確認系統還活著。

## 設定步驟

### 1. 取得 Telegram chat id

1. 在 Telegram 打開你的 bot（t.me/airline_ticket_3301_bot），按 **Start** 或傳 `/start`
2. 瀏覽器打開下面網址（把 `<TOKEN>` 換成 bot token）：
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. 找到 `"chat":{"id":123456789,...}`，那串數字就是 chat id

（沒設定 chat id 也行：程式會自動從最近傳給 bot 的訊息找出來，但 Telegram 只保留 24 小時，建議還是設好。）

### 2. 設定 GitHub Secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**：

| Secret | 必填 | 內容 |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | ✅ | BotFather 給的 token |
| `TELEGRAM_CHAT_ID` | 建議 | 上一步拿到的數字 |
| `TRAVELPAYOUTS_TOKEN` | 選填 | [travelpayouts.com](https://www.travelpayouts.com/) 註冊後在 Profile → API token 取得 |

⚠️ **不要把 token 寫進程式碼或 commit 進 repo**。如果 token 曾經貼在公開的地方，到 BotFather 用 `/revoke` 換一組新的。

### 3. 測試 & 啟用

1. Repo → **Actions** → 「Check fares TPE → KIX」→ **Run workflow**，mode 選 `test-telegram` → 應該會收到測試訊息
2. 再跑一次 mode 選 `check`，看 log 有沒有印出各日期的最低價
3. 之後每 3 小時會自動執行

## 可調整的參數

在 **Settings → Secrets and variables → Actions → Variables** 新增即可覆寫（本機執行則用環境變數）：

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `MAX_PRICE` | 3500 | 低於這個價格（TWD）就通知 |
| `MAX_STOPS` | 0 | 0 = 只看直飛；1 = 可接受轉機一次 |
| `DAYS_AHEAD_MAX` | 120 | 監控未來幾天內出發的航班 |
| `DATES_PER_RUN` | 12 | 每次執行查幾個日期（越大越快掃完，但請求越多） |
| `SUMMARY_HOUR` | 21 | 台北時間幾點後發每日摘要，設 `-1` 關閉 |

更多參數（`DELAY_MIN`、`DELAY_MAX`、`HOT_MARGIN`…）見 `ticket_watch/config.py`。

## 本機執行

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
python -m ticket_watch --test-telegram   # 測試 Telegram
python -m ticket_watch --date 2026-11-18 # 只查一天、印出結果（不通知）
python -m ticket_watch                   # 正式執行一輪
DRY_RUN=1 python -m ticket_watch         # 只印訊息、不真的發送
```

如果 Google 擋 GitHub Actions 的機房 IP（收到「連續 N 輪查不到」警告），可以改在家裡電腦或 NAS 用 cron 執行，住宅 IP 幾乎不會被擋：

```cron
17 */3 * * * cd /path/to/airline_ticket && TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python3 -m ticket_watch >> watch.log 2>&1
```

## 注意事項

- Google Flights 顯示的是含稅票價，**不含托運行李、選位**等加購費用。
- 私有 repo 的 GitHub Actions 每月有 2,000 分鐘免費額度；這個排程每月約用 1,000–1,500 分鐘。公開 repo 不限。
- 公開 repo 若 60 天沒有任何 commit，GitHub 會自動停用排程，需要到 Actions 頁面重新啟用。
- 這是用非官方方式讀取 Google Flights 網頁，Google 改版時可能失效，屆時會收到 Telegram 警告。

## 開發

```bash
pip install -r requirements.txt pytest
python -m pytest
```
