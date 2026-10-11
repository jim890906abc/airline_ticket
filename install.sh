#!/usr/bin/env bash
# 安裝 / 更新（同一支腳本，重複執行安全）：
#   下載最新程式 → 安裝套件 → 寫設定 → 設 cron 看門狗 → （重新）啟動網站與背景掃描
#
# 第一次安裝：
#   curl -fsSL https://raw.githubusercontent.com/jim890906abc/airline_ticket/claude/airline-ticket-notifier-5i70uz/install.sh | TELEGRAM_BOT_TOKEN=xxx bash
# 之後更新：
#   cd ~/airline_ticket && git fetch -q origin claude/airline-ticket-notifier-5i70uz && git reset -q --hard FETCH_HEAD && bash install.sh
#
# 可選環境變數：TELEGRAM_CHAT_ID、TRAVELPAYOUTS_TOKEN、MAX_PRICE、WEB_PORT、WEB_PASSWORD、INSTALL_DIR
set -euo pipefail

REPO="https://github.com/jim890906abc/airline_ticket.git"
BRANCH="${BRANCH:-claude/airline-ticket-notifier-5i70uz}"
DIR="${INSTALL_DIR:-$HOME/airline_ticket}"

die() { echo "❌ $*" >&2; exit 1; }
env_get() { [ -f "$DIR/.env" ] && sed -n "s/^$1=//p" "$DIR/.env" | head -1 || true; }

command -v git >/dev/null || die "需要 git"
command -v python3 >/dev/null || die "需要 python3（3.10 以上）"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 版本太舊，需要 3.10 以上"
command -v crontab >/dev/null || die "需要 crontab（Debian/Ubuntu: sudo apt install cron）"

# 更新時沿用舊設定（環境變數有給就以環境變數為準）
FIRST_INSTALL=1
[ -f "$DIR/.env" ] && FIRST_INSTALL=0
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-$(env_get TELEGRAM_BOT_TOKEN)}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-$(env_get TELEGRAM_CHAT_ID)}"
TRAVELPAYOUTS_TOKEN="${TRAVELPAYOUTS_TOKEN:-$(env_get TRAVELPAYOUTS_TOKEN)}"
MAX_PRICE="${MAX_PRICE:-$(env_get MAX_PRICE)}"
MAX_PRICE="${MAX_PRICE:-3500}"
WEB_PORT="${WEB_PORT:-$(env_get WEB_PORT)}"
WEB_PORT="${WEB_PORT:-8787}"
WEB_PASSWORD="${WEB_PASSWORD:-$(env_get WEB_PASSWORD)}"
WEB_PASSWORD="${WEB_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(9))')}"
[ -n "$TELEGRAM_BOT_TOKEN" ] || die "請設定 TELEGRAM_BOT_TOKEN，例如：curl ... | TELEGRAM_BOT_TOKEN=xxx bash"

echo "📥 取得最新程式 → $DIR"
if [ -d "$DIR/.git" ]; then
  # 直接對齊遠端版本，不做 merge，不會有衝突；.env / state / log 都不在版控內，不受影響
  git -C "$DIR" fetch -q origin "$BRANCH"
  git -C "$DIR" checkout -q -B "$BRANCH" FETCH_HEAD
  git -C "$DIR" reset -q --hard FETCH_HEAD
else
  git clone -q -b "$BRANCH" "$REPO" "$DIR"
fi
cd "$DIR"
echo "   版本：$(git log -1 --format='%h %s')"

echo "📦 安裝套件"
[ -x .venv/bin/python ] || python3 -m venv .venv || die "建立 venv 失敗（Debian/Ubuntu: sudo apt install python3-venv）"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [ -z "$TELEGRAM_CHAT_ID" ]; then
  echo "🔎 從 bot 最近收到的訊息找 chat id"
  TELEGRAM_CHAT_ID="$(TELEGRAM_BOT_TOKEN="$TELEGRAM_BOT_TOKEN" .venv/bin/python -c 'import os; from ticket_watch.telegram import Telegram; print(Telegram(os.environ["TELEGRAM_BOT_TOKEN"]).discover_chat_id())')"
  [ -n "$TELEGRAM_CHAT_ID" ] || die "找不到 chat id：請先在 Telegram 對 bot 傳 /start，再重跑一次"
fi
echo "   chat id = $TELEGRAM_CHAT_ID"

umask 077
cat > .env <<EOF
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
TRAVELPAYOUTS_TOKEN=$TRAVELPAYOUTS_TOKEN
MAX_PRICE=$MAX_PRICE
WEB_PORT=$WEB_PORT
WEB_PASSWORD=$WEB_PASSWORD
EOF
umask 022

cat > run.sh <<'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")"
set -a; . ./.env; set +a
exec .venv/bin/python -m ticket_watch "$@"
EOF
chmod +x run.sh

if [ "$FIRST_INSTALL" = 1 ]; then
  echo "📨 發送測試訊息"
  ./run.sh --test-telegram || die "Telegram 測試失敗，請檢查 token"
fi

echo "⏰ 設定 cron 看門狗（每 5 分鐘確認服務活著，掛掉或重開機會自動拉起）"
( crontab -l 2>/dev/null | grep -vF "$DIR/run.sh" || true
  echo "*/5 * * * * $DIR/run.sh serve >> $DIR/service.log 2>&1" ) | crontab -

# 停掉舊的服務（更新時），再啟動新版
if [ -f state/bot.lock ]; then
  OLD_PID="$(cat state/bot.lock 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null && ps -p "$OLD_PID" -o args= | grep -q ticket_watch; then
    echo "🔁 停止舊版服務（pid $OLD_PID）"
    kill "$OLD_PID" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$OLD_PID" 2>/dev/null || break; sleep 0.5; done
    kill -9 "$OLD_PID" 2>/dev/null || true
  fi
fi
echo "🚀 啟動網站與背景掃描（紀錄：$DIR/service.log）"
nohup ./run.sh serve >> service.log 2>&1 &
PID=$!
OK=0
for _ in $(seq 1 20); do
  sleep 1
  kill -0 "$PID" 2>/dev/null || break
  if command -v curl >/dev/null; then
    CODE="$(curl -s -o /dev/null -w '%{http_code}' --noproxy '*' -u "admin:$WEB_PASSWORD" "http://127.0.0.1:$WEB_PORT/api/overview" || true)"
    [ "$CODE" = 200 ] && { OK=1; break; }
  else
    OK=1; break
  fi
done
[ "$OK" = 1 ] || { tail -20 service.log; die "服務啟動失敗，請看上面的紀錄"; }
LAN_IP="$(.venv/bin/python -c 'from ticket_watch.main import lan_ip; print(lan_ip())' 2>/dev/null || echo 127.0.0.1)"

cat <<EOF

✅ 完成！打開網站設定要通知的日期區間與價格：

   🌐 本機：      http://127.0.0.1:$WEB_PORT
   🌐 同網路手機：http://$LAN_IP:$WEB_PORT
   🔑 帳號隨便填，密碼：$WEB_PASSWORD   （存在 $DIR/.env）

   Telegram 只負責接收低價通知。
   看紀錄：tail -f $DIR/service.log
   更新：  cd $DIR && git fetch -q origin $BRANCH && git reset -q --hard FETCH_HEAD && bash install.sh
   移除：  crontab -l | grep -vF "$DIR/run.sh" | crontab - ; kill \$(cat $DIR/state/bot.lock) ; rm -rf "$DIR"
EOF
