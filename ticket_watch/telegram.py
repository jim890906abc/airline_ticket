from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class Telegram:
    def __init__(self, token: str, chat_id: str = "", dry_run: bool = False):
        self.token = token
        self.chat_id = chat_id
        self.dry_run = dry_run

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def discover_chat_id(self) -> str:
        """沒設定 TELEGRAM_CHAT_ID 時，從最近傳給 bot 的訊息找出 chat id。

        先在 Telegram 對 bot 傳一則 /start，24 小時內執行即可找到。
        """
        res = requests.get(self._api("getUpdates"), timeout=30)
        res.raise_for_status()
        for update in reversed(res.json().get("result", [])):
            msg = update.get("message") or update.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if "id" in chat:
                return str(chat["id"])
        return ""

    def send(self, text: str) -> bool:
        if self.dry_run:
            log.info("[DRY_RUN] Telegram 訊息：\n%s", text)
            return True
        if not self.token:
            log.error("未設定 TELEGRAM_BOT_TOKEN，無法發送通知")
            return False
        if not self.chat_id:
            try:
                self.chat_id = self.discover_chat_id()
            except Exception as e:
                log.error("取得 chat id 失敗：%s", e)
            if not self.chat_id:
                log.error("找不到 chat id：請先在 Telegram 對 bot 傳送 /start")
                return False
            log.warning(
                "自動找到 TELEGRAM_CHAT_ID=%s，建議把它設成 secret 以免之後找不到",
                self.chat_id,
            )
        try:
            res = requests.post(
                self._api("sendMessage"),
                json={
                    "chat_id": self.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
                timeout=30,
            )
            if res.status_code != 200:
                log.error("Telegram 發送失敗：HTTP %s %s", res.status_code, res.text[:200])
                return False
            return True
        except Exception as e:
            log.error("Telegram 發送失敗：%s", e)
            return False
