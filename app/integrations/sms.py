"""SMS shlyuz: eskiz.uz (O'zbekiston) yoki console (ishlab chiqish)."""
from __future__ import annotations

import logging
import re
import threading
from typing import Protocol

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class SmsResult:
    def __init__(self, ok: bool, ref: str | None = None, error: str | None = None):
        self.ok, self.ref, self.error = ok, ref, error


class SmsProvider(Protocol):
    def send(self, phone: str, text: str) -> SmsResult: ...


def normalize_phone(phone: str) -> str:
    """+998 90 123-45-67 -> 998901234567"""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 9:
        digits = "998" + digits
    return digits


class ConsoleSms:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []

    def send(self, phone, text):
        self.sent.append((normalize_phone(phone), text))
        log.info("SMS -> %s: %s", phone, text)
        return SmsResult(True, ref=f"console-{len(self.sent)}")


class EskizSms:
    BASE = "https://notify.eskiz.uz/api"

    def __init__(self, email: str, password: str, sender: str):
        self.email, self.password, self.sender = email, password, sender
        self._token: str | None = None
        self._lock = threading.Lock()

    def _login(self) -> str:
        r = httpx.post(f"{self.BASE}/auth/login", data={"email": self.email, "password": self.password}, timeout=15)
        r.raise_for_status()
        return r.json()["data"]["token"]

    def send(self, phone, text):
        try:
            with self._lock:
                if self._token is None:
                    self._token = self._login()
                token = self._token
            payload = {"mobile_phone": normalize_phone(phone), "message": text, "from": self.sender}
            r = httpx.post(
                f"{self.BASE}/message/sms/send",
                data=payload,
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            if r.status_code == 401:
                with self._lock:
                    self._token = self._login()
                r = httpx.post(
                    f"{self.BASE}/message/sms/send",
                    data=payload,
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=15,
                )
            if r.status_code >= 400:
                return SmsResult(False, error=f"{r.status_code}: {r.text[:200]}")
            data = r.json()
            return SmsResult(True, ref=str(data.get("id") or data.get("message_id") or ""))
        except (httpx.HTTPError, KeyError, ValueError) as e:
            log.error("Eskiz SMS xato: %s", e)
            return SmsResult(False, error=str(e))


_provider: SmsProvider | None = None


def provider() -> SmsProvider:
    global _provider
    if _provider is None:
        s = get_settings()
        if s.sms_provider == "eskiz":
            _provider = EskizSms(s.sms_eskiz_email, s.sms_eskiz_password, s.sms_eskiz_from)
        else:
            _provider = ConsoleSms()
    return _provider


def set_provider(p: SmsProvider | None) -> None:
    global _provider
    _provider = p
