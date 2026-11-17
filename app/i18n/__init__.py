"""Tarjimalar: qoraqalpoq (kaa), o'zbek (uz), rus (ru), ingliz (en).

Mehmon sahifasi — 4 til, xodimlar boti — kaa/uz/ru (TT 13-bo'lim).
Qoraqalpoqcha matnlar pilotdan oldin ona tilida so'zlashuvchi tomonidan
tekshirilishi kerak (docs/TODO.md).
"""
from __future__ import annotations

from app.i18n.messages import MESSAGES

LANGS = ("kaa", "uz", "ru", "en")
STAFF_LANGS = ("kaa", "uz", "ru")
DEFAULT = "uz"
LANG_NAMES = {"kaa": "Qaraqalpaqsha", "uz": "O'zbekcha", "ru": "Русский", "en": "English"}


def t(key: str, lang: str | None = None, **kw) -> str:
    lang = lang if lang in LANGS else DEFAULT
    entry = MESSAGES.get(key)
    if entry is None:
        return key
    text = entry.get(lang) or entry.get(DEFAULT) or entry.get("ru") or key
    if kw:
        try:
            return text.format(**kw)
        except (KeyError, IndexError):
            return text
    return text


def pick_lang(accept_language: str | None, explicit: str | None = None) -> str:
    if explicit in LANGS:
        return explicit
    if accept_language:
        for part in accept_language.split(","):
            code = part.split(";")[0].strip().lower()
            if code.startswith("kaa"):
                return "kaa"
            for lang in ("uz", "ru", "en"):
                if code.startswith(lang):
                    return lang
    return DEFAULT


def money(amount: int | None, lang: str | None = None) -> str:
    if amount is None:
        return "—"
    unit = {"ru": "сум", "en": "UZS", "kaa": "swm"}.get(lang or DEFAULT, "so'm")
    return f"{amount:,}".replace(",", " ") + f" {unit}"
