"""Telegram Bot API ga to'g'ridan-to'g'ri (aiogram'siz) xabar yuborish.

Veb-server va rejalashtiruvchi ogohlantirishlarni shu orqali yuboradi;
interaktiv bot (aiogram) alohida jarayonda ishlaydi.
"""
from __future__ import annotations

import logging
from typing import Protocol

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class TelegramBackend(Protocol):
    def send_message(self, chat_id: int, text: str, buttons: list[list[dict]] | None = None) -> int | None: ...

    def edit_message(self, chat_id: int, message_id: int, text: str) -> None: ...

    def send_photo(self, chat_id: int, photo: str, caption: str = "") -> int | None: ...


class HttpTelegram:
    def __init__(self, token: str, api_base: str):
        self.token = token
        self.api_base = api_base.rstrip("/")

    def _call(self, method: str, payload: dict) -> dict | None:
        if not self.token:
            log.warning("TELEGRAM token yo'q, %s yuborilmadi: %s", method, payload.get("text", "")[:80])
            return None
        try:
            r = httpx.post(f"{self.api_base}/bot{self.token}/{method}", json=payload, timeout=10)
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            log.error("Telegram %s xato: %s", method, e)
            return None
        if not data.get("ok"):
            log.error("Telegram %s rad etdi: %s", method, data)
            return None
        return data.get("result")

    def send_message(self, chat_id, text, buttons=None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        res = self._call("sendMessage", payload)
        return res.get("message_id") if res else None

    def edit_message(self, chat_id, message_id, text):
        self._call(
            "editMessageText",
            {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"},
        )

    def send_photo(self, chat_id, photo, caption=""):
        res = self._call("sendPhoto", {"chat_id": chat_id, "photo": photo, "caption": caption, "parse_mode": "HTML"})
        return res.get("message_id") if res else None


class FakeTelegram:
    """Testlar va lokal ishlab chiqish uchun: xabarlarni xotirada saqlaydi."""

    def __init__(self):
        self.sent: list[dict] = []
        self._next_id = 1

    def send_message(self, chat_id, text, buttons=None):
        mid = self._next_id
        self._next_id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons, "message_id": mid})
        return mid

    def edit_message(self, chat_id, message_id, text):
        self.sent.append({"chat_id": chat_id, "text": text, "edit_of": message_id})

    def send_photo(self, chat_id, photo, caption=""):
        return self.send_message(chat_id, caption)


_backend: TelegramBackend | None = None


def backend() -> TelegramBackend:
    global _backend
    if _backend is None:
        s = get_settings()
        _backend = HttpTelegram(s.telegram_bot_token, s.telegram_api_base)
    return _backend


def set_backend(b: TelegramBackend | None) -> None:
    global _backend
    _backend = b
