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

    def call(self, method: str, payload: dict | None = None, timeout: float = 30):
        """呼叫任意 Bot API 方法，成功回傳 result，失敗回傳 None。"""
        if self.dry_run:
            log.info("[DRY_RUN] %s %s", method, payload)
            return True
        try:
            res = requests.post(self._api(method), json=payload or {}, timeout=timeout)
            body = res.json()
        except Exception as e:
            log.error("Telegram %s 失敗：%s", method, e)
            return None
        if not body.get("ok"):
            log.error("Telegram %s 失敗：%s", method, str(body)[:300])
            return None
        return body.get("result")

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

    def get_updates(self, offset: int, timeout: int = 50) -> list[dict]:
        """Long polling：最多等 timeout 秒，有新訊息/按鈕就立刻回來。"""
        res = requests.post(
            self._api("getUpdates"),
            json={
                "offset": offset,
                "timeout": timeout,
                "allowed_updates": ["message", "callback_query"],
            },
            timeout=timeout + 15,
        )
        body = res.json()
        if not body.get("ok"):
            raise RuntimeError(str(body)[:300])
        return body.get("result", [])

    def send(self, text: str, reply_markup: dict | None = None) -> bool:
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
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload) is not None


class MutedTelegram:
    """暫停通知時使用：不發送，且回傳 False 讓班機不被標記為已通知，恢復後仍會通知。"""

    def send(self, text: str, reply_markup: dict | None = None) -> bool:
        log.info("通知已暫停，略過訊息：%s", text.splitlines()[0] if text else "")
        return False
