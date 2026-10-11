"""命令列進入點。

    python -m ticket_watch serve          # 常駐：網站 + 背景定時掃描 + Telegram 通知
    python -m ticket_watch check          # 掃描一輪就結束（GitHub Actions 用）
    python -m ticket_watch --test-telegram
    python -m ticket_watch --date 2026-11-18 [--route KIX-TPE]
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
from datetime import date, datetime, timezone

from .config import Config
from .models import Fare
from .rules import Settings
from .state import State
from .telegram import Telegram

log = logging.getLogger("ticket_watch")


def lan_ip() -> str:
    """本機在區網的 IP（不會真的送封包）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def make_tp_fetch(cfg: Config):
    if not cfg.travelpayouts_token:
        return None
    from .travelpayouts import fetch_cheapest

    def tp_fetch(origin: str, destination: str, start: date, end: date) -> list[Fare]:
        return fetch_cheapest(
            cfg.travelpayouts_token, origin, destination, cfg.currency, start, end, direct_only=cfg.max_stops == 0
        )

    return tp_fetch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="桃園 ⇄ 大阪 低價機票監控")
    parser.add_argument(
        "command",
        nargs="?",
        default="check",
        choices=["check", "serve", "bot"],
        help="serve = 常駐網站＋定時掃描；check = 掃描一輪就結束（預設）",
    )
    parser.add_argument("--test-telegram", action="store_true", help="發一則測試訊息")
    parser.add_argument("--date", help="只查單一日期並印出結果（除錯用，不通知）")
    parser.add_argument("--route", default="TPE-KIX", help="搭配 --date，例：KIX-TPE")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    cfg = Config.from_env()
    tg = Telegram(cfg.telegram_token, cfg.telegram_chat_id, dry_run=cfg.dry_run)

    if args.test_telegram:
        ok = tg.send("👋 機票監控測試訊息：Telegram 通知運作正常")
        if ok and tg.chat_id:
            log.info("測試訊息已送出（chat id：%s）", tg.chat_id)
        return 0 if ok else 1

    from .google_flights import GoogleFlightsClient

    gf = GoogleFlightsClient(cfg.currency, cfg.max_stops)

    if args.date:
        origin, _, destination = args.route.upper().partition("-")
        for f in gf.search(origin, destination, args.date):
            print(f.price, f.airline, f.depart_time, "->", f.arrive_time, f"stops={f.stops}")
        return 0

    if args.command in ("serve", "bot"):  # bot = 舊版名稱，相容 cron
        return run_server(cfg, tg, gf.search)

    from .scanner import scan_round

    settings = Settings(os.path.join(os.path.dirname(cfg.state_file) or ".", "settings.json"), cfg.max_price)
    state = State.load(cfg.state_file)
    import random
    import time

    def paced(origin: str, destination: str, d: str) -> list[Fare]:
        time.sleep(random.uniform(cfg.delay_min, cfg.delay_max))
        return gf.search(origin, destination, d)

    scan_round(
        cfg,
        state,
        settings.enabled_rules(),
        settings.dates_per_run,
        paced,
        tg,
        datetime.now(timezone.utc),
        tp_fetch=make_tp_fetch(cfg),
    )
    state.save(cfg.state_file)
    return 0


def start_tunnel(cfg: Config, svc, state_dir: str, tell_user):
    """依設定啟動 Cloudflare Tunnel，回傳 Tunnel 或 None。"""
    from .tunnel import Tunnel, find_binary

    if cfg.tunnel == "off" and not cfg.cf_tunnel_token:
        return None
    if not cfg.web_password:
        log.error("沒有設定 WEB_PASSWORD，為了安全不開公網網址")
        return None
    binary = find_binary(os.getcwd())
    if not binary:
        log.error("找不到 cloudflared，不開公網網址（重跑 install.sh 會自動下載）")
        return None
    if cfg.cf_tunnel_token and not cfg.public_url:
        log.warning("有 CF_TUNNEL_TOKEN 但沒設定 PUBLIC_URL，網站上會看不到網址")

    def on_url(url: str) -> None:
        if cfg.cf_tunnel_token:
            tell_user(f"🌐 機票監控網站已上線：\n{url}")
        else:
            tell_user(
                f"🌐 機票監控網站的新網址：\n{url}\n\n"
                "（Cloudflare 快速通道每次重啟網址都會變，變了會再通知你；密碼在 ~/airline_ticket/.env）"
            )

    tunnel = Tunnel(binary, cfg.web_port, state_dir, on_url, token=cfg.cf_tunnel_token, public_url=cfg.public_url)
    svc.tunnel = tunnel
    tunnel.start()
    return tunnel


def run_server(cfg: Config, tg: Telegram, search) -> int:
    import fcntl

    from .service import Service
    from .web import serve

    if not cfg.telegram_token or not tg.chat_id:
        log.warning("沒有設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID，將不會發送通知")

    # 同時只允許一個服務在跑（cron 每 5 分鐘呼叫一次當作看門狗）
    state_dir = os.path.dirname(cfg.state_file) or "."
    os.makedirs(state_dir, exist_ok=True)
    lock = open(os.path.join(state_dir, "bot.lock"), "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # 已經有一個在跑了
    lock.seek(0)
    lock.truncate()
    lock.write(str(os.getpid()))  # 給 install.sh 更新時重啟用
    lock.flush()

    svc = Service(cfg, tg, search, tp_fetch=make_tp_fetch(cfg), install_dir=os.getcwd())
    try:
        httpd = serve(svc, cfg.web_host, cfg.web_port, cfg.web_password)
    except OSError as e:
        log.error("網站無法啟動（port %s 被佔用？）：%s", cfg.web_port, e)
        return 1

    lan_url = f"http://{lan_ip() if cfg.web_host in ('0.0.0.0', '') else cfg.web_host}:{cfg.web_port}"

    def tell_user(text: str) -> None:
        """發網址給使用者；第一次順便清掉舊版 Telegram 按鈕選單。"""
        if not (tg.token and tg.chat_id) or cfg.dry_run:
            return
        first = not svc.settings.keyboard_removed
        if first:
            tg.call("deleteMyCommands")
        if tg.send(text, reply_markup={"remove_keyboard": True} if first else None) and first:
            svc.settings.keyboard_removed = True
            svc.settings.save()

    tunnel = start_tunnel(cfg, svc, state_dir, tell_user)
    if tunnel is None:
        if not svc.settings.keyboard_removed:
            tell_user(f"🌐 機票監控網站：\n{lan_url}\n\nTelegram 之後只負責接收低價通知。")

    def shutdown(signum, frame):
        if tunnel:
            tunnel.stop()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, shutdown)  # 不處理 SIGHUP：nohup 啟動時要維持忽略，關掉終端機才不會停

    svc.start()
    log.info("網站啟動：%s ，下次掃描 %s", lan_url, svc.next_scan.isoformat(timespec="minutes"))
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        if tunnel:
            tunnel.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
