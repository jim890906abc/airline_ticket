# 桃園 ⇄ 大阪 機票監控（網站 + Telegram 通知）

在自己電腦上架一個網站，監控 **桃園 (TPE) ⇄ 大阪關西 (KIX)** 雙向單程機票：

- 🌐 **網站**：即時查詢日期區間的最低價、價格日曆、來回組合、設定通知規則
- 🌍 **公網網址**：自動用 Cloudflare Tunnel 產生 `https://xxxx.trycloudflare.com`，在外面用手機也能開（有密碼保護）
- 📨 **Telegram**：只負責接收「符合你設定的日期區間與價格」的低價通知（以及新的公網網址）

> 大阪的伊丹機場 (ITM) 只飛日本國內線，從桃園出發的國際線都降關西 (KIX)，所以只查 KIX。

## 安裝（一行指令）

Mac / Linux / WSL，需要 Python 3.10+、git、cron。先在 Telegram 對 bot 按 **Start**，然後執行：

```bash
curl -fsSL https://raw.githubusercontent.com/jim890906abc/airline_ticket/claude/airline-ticket-notifier-5i70uz/install.sh | TELEGRAM_BOT_TOKEN=你的token bash
```

會自動：下載到 `~/airline_ticket` → 安裝套件 → 找出 chat id → 發測試訊息 → 下載 cloudflared → 設 cron 看門狗 → 啟動網站與公網通道。
最後會印出 **公網網址**、區網網址和網站密碼。

## 更新（一行指令）

```bash
cd ~/airline_ticket && git fetch -q origin claude/airline-ticket-notifier-5i70uz && git reset -q --hard FETCH_HEAD && bash install.sh
```

直接對齊 GitHub 上的最新版本（不 merge，所以不會有衝突），保留 token、網站密碼、通知規則、價格紀錄，並自動重啟服務。
網站「安裝 / 系統」分頁也有這兩行指令可以一鍵複製。

## 網站功能

| 分頁 | 功能 |
| --- | --- |
| 總覽 | 兩個方向各自目前已知最便宜 10 張、掃描狀態、立即掃描、目前進行中的查詢進度 |
| 價格日曆 | 未來 330 天每天最低價的色塊月曆（越深越便宜，★ = 低於通知門檻），點日期看當天所有航班或即時查詢 |
| 即時查詢 | 選方向（去 / 回 / 雙向）、日期區間、星期，背景逐日查 Google Flights，進度與結果即時顯示；可沿用幾小時內的紀錄省下請求 |
| 來回組合 | 兩張單程自由搭配：指定去程區間與停留天數，算出最便宜的來回組合；缺資料時一鍵補查 |
| 通知設定 | 新增多條規則：方向、指定日期區間或「未來 N 天」、低於多少錢、只看星期幾；一鍵暫停 / 恢復、發測試訊息 |
| 安裝 / 系統 | 安裝 / 更新指令、目前版本、背景掃描頻率設定、最近的工作 |

## 公網網址（Cloudflare Tunnel）

預設使用 Cloudflare **快速通道**：不用註冊、不用網域、不用開 port，安裝完就有 `https://xxxx.trycloudflare.com`。

- 網址是隨機的，**服務重啟（更新、重開機、cloudflared 斷線）後會換新網址**，換了會自動傳到 Telegram，網站「安裝 / 系統」頁也看得到
- Cloudflare 對快速通道不保證穩定性；cloudflared 掛掉或連不上會自動重啟（每 10 分鐘從外面檢查一次 `/healthz`）
- 網站用 HTTP Basic 密碼保護（帳號隨便填，密碼在 `~/airline_ticket/.env` 的 `WEB_PASSWORD`）；同一個 IP 15 分鐘內錯 10 次會被暫時封鎖

### 想要固定網址

需要 Cloudflare 帳號和一個託管在 Cloudflare 的網域：

1. Cloudflare 後台 → Zero Trust → Networks → Tunnels → Create a tunnel（Cloudflared）
2. Public Hostname 填例如 `fare.你的網域.com`，Service 填 `http://localhost:8787`
3. 複製畫面上 `--token` 後面那串，然後執行：

```bash
cd ~/airline_ticket && git fetch -q origin claude/airline-ticket-notifier-5i70uz && git reset -q --hard FETCH_HEAD && CF_TUNNEL_TOKEN=那串token PUBLIC_URL=https://fare.你的網域.com bash install.sh
```

不想要公網網址：`TUNNEL=off bash install.sh`（之後更新會沿用這個設定）。

## 怎麼抓才有效又不會被 ban

| 做法 | 說明 |
| --- | --- |
| **用 Google Flights 當來源** | 已聚合虎航、樂桃、捷星、酷航、華航、長榮等所有航空，一個請求涵蓋全部。直接打各家廉航官網反而容易觸發防爬。 |
| **只查你在意的日期** | 背景只掃「啟用中通知規則」涵蓋的日期，沒有規則就不掃。 |
| **少量、分散** | 預設每 3 小時（±15 分鐘隨機）掃 12 個日期，日期順序打亂，一天約 96 個請求。可在網站調整。 |
| **全域請求閘門** | 背景掃描與網站即時查詢都排同一個隊，任兩個請求之間隨機間隔 8–20 秒，狂按也不會爆量。 |
| **聰明挑日期** | 沒查過、最久沒查的優先；價格接近門檻的日期更頻繁重查。即時查詢可沿用近期紀錄。 |
| **異常立刻停手** | HTTP 429/403 立刻停止；連續 2 個失敗也停止。連續 3 輪失敗發一次 Telegram 警告，恢復時再通知。 |
| **（選用）Travelpayouts 官方 API** | 一個請求拿到整個月每天的最低價快取，零封鎖風險，用來優先挑出可能便宜的日期。 |
| **不重複通知** | 同一班機只通知一次；更便宜、或漲回去又降下來才會再通知。 |

## 常用指令

```bash
tail -f ~/airline_ticket/service.log                    # 看紀錄
cat ~/airline_ticket/state/public_url                   # 目前的公網網址
~/airline_ticket/run.sh --date 2026-11-18               # 命令列查單日（不通知）
~/airline_ticket/run.sh --date 2026-11-22 --route KIX-TPE
~/airline_ticket/run.sh --test-telegram                 # 發測試訊息
```

移除：

```bash
crontab -l | grep -vF ~/airline_ticket/run.sh | crontab - ; kill $(cat ~/airline_ticket/state/bot.lock) ; rm -rf ~/airline_ticket
```

## 設定（`~/airline_ticket/.env`）

改完再跑一次更新指令即可生效。

| 變數 | 預設 | 說明 |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | BotFather 給的 token（必填） |
| `TELEGRAM_CHAT_ID` | 自動偵測 | 你的 chat id |
| `WEB_PORT` | 8787 | 網站 port |
| `WEB_PASSWORD` | 安裝時隨機產生 | 網站密碼 |
| `TUNNEL` | quick | `quick` = Cloudflare 快速通道；`off` = 不開公網 |
| `CF_TUNNEL_TOKEN` / `PUBLIC_URL` | — | 固定網址用的 Cloudflare Tunnel token 與網址 |
| `TRAVELPAYOUTS_TOKEN` | — | 選填，[travelpayouts.com](https://www.travelpayouts.com/) Profile → API token |
| `MAX_PRICE` | 3500 | 第一次安裝時預設規則的門檻（之後以網站為準） |
| `MAX_STOPS` | 0 | 0 = 只看直飛 |

更多參數見 `ticket_watch/config.py`。

⚠️ 不要把 token commit 進 repo。token 若曾外流，到 BotFather 用 `/revoke` 換新的，再用 `TELEGRAM_BOT_TOKEN=新token` 跑一次安裝指令。

## 不想開電腦？用 GitHub Actions

`.github/workflows/check-fares.yml` 可以在 GitHub 上定時掃描並通知（**沒有網站**，規則用預設：桃園→大阪未來 120 天 < 3500）。
到 repo Settings → Secrets 設 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`，再把 workflow 裡的 `schedule` 取消註解即可。
不要和家裡的服務同時開，會重複查詢。

## 注意事項

- 價格為 Google Flights 顯示的含稅票價，**不含托運行李、選位**等加購。
- 這是用非官方方式讀取 Google Flights 網頁，Google 改版時可能失效，屆時會收到 Telegram 警告。

## 開發

```bash
pip install -r requirements.txt pytest
python -m pytest
```
