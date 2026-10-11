#!/usr/bin/env bash
# 一鍵安裝：下載程式 → 建 venv → 找 Telegram chat id → 發測試訊息 → 設 cron 每 3 小時執行
#
#   curl -fsSL https://raw.githubusercontent.com/jim890906abc/airline_ticket/claude/airline-ticket-notifier-5i70uz/install.sh | TELEGRAM_BOT_TOKEN=xxx bash
#
# 可選環境變數：TELEGRAM_CHAT_ID、TRAVELPAYOUTS_TOKEN、MAX_PRICE、INSTALL_DIR
set -euo pipefail

REPO="https://github.com/jim890906abc/airline_ticket.git"
BRANCH="${BRANCH:-claude/airline-ticket-notifier-5i70uz}"
DIR="${INSTALL_DIR:-$HOME/airline_ticket}"

die() { echo "❌ $*" >&2; exit 1; }

[ -n "${TELEGRAM_BOT_TOKEN:-}" ] || die "請設定 TELEGRAM_BOT_TOKEN，例如：curl ... | TELEGRAM_BOT_TOKEN=xxx bash"
command -v git >/dev/null || die "需要 git"
command -v python3 >/dev/null || die "需要 python3（3.10 以上）"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' || die "Python 版本太舊，需要 3.10 以上"
command -v crontab >/dev/null || die "需要 crontab（Debian/Ubuntu: sudo apt install cron）"

echo "📥 下載程式到 $DIR"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" fetch -q origin "$BRANCH"
  git -C "$DIR" checkout -q "$BRANCH"
  git -C "$DIR" pull -q --ff-only origin "$BRANCH"
else
  git clone -q -b "$BRANCH" "$REPO" "$DIR"
fi
cd "$DIR"

echo "📦 安裝套件"
python3 -m venv .venv || die "建立 venv 失敗（Debian/Ubuntu: sudo apt install python3-venv）"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

if [ -z "${TELEGRAM_CHAT_ID:-}" ] && [ -f .env ]; then
  TELEGRAM_CHAT_ID="$(sed -n 's/^TELEGRAM_CHAT_ID=//p' .env)"
fi
if [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "🔎 從 bot 最近收到的訊息找 chat id"
  TELEGRAM_CHAT_ID="$(.venv/bin/python -c 'import os; from ticket_watch.telegram import Telegram; print(Telegram(os.environ["TELEGRAM_BOT_TOKEN"]).discover_chat_id())')"
  [ -n "$TELEGRAM_CHAT_ID" ] || die "找不到 chat id：請先在 Telegram 對 bot 傳 /start，再重跑一次"
fi
echo "   chat id = $TELEGRAM_CHAT_ID"

umask 077
cat > .env <<EOF
TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=$TELEGRAM_CHAT_ID
TRAVELPAYOUTS_TOKEN=${TRAVELPAYOUTS_TOKEN:-}
MAX_PRICE=${MAX_PRICE:-3500}
EOF
umask 022

cat > run.sh <<'EOF'
#!/usr/bin/env bash
cd "$(dirname "$0")"
set -a; . ./.env; set +a
exec .venv/bin/python -m ticket_watch "$@"
EOF
chmod +x run.sh

echo "📨 發送測試訊息"
./run.sh --test-telegram || die "Telegram 測試失敗，請檢查 token"

echo "⏰ 設定 cron（每 3 小時）"
( crontab -l 2>/dev/null | grep -vF "$DIR/run.sh" || true
  echo "17 */3 * * * $DIR/run.sh >> $DIR/watch.log 2>&1" ) | crontab -

echo "🚀 背景執行第一輪查詢（紀錄：$DIR/watch.log）"
nohup ./run.sh >> watch.log 2>&1 &

cat <<EOF

✅ 完成！之後每 3 小時自動查一次 TPE → KIX，低於 NT\$${MAX_PRICE:-3500} 會發 Telegram。
   看紀錄：tail -f $DIR/watch.log
   查單日：$DIR/run.sh --date 2026-11-18
   移除：  crontab -l | grep -vF "$DIR/run.sh" | crontab - && rm -rf "$DIR"
EOF
