"""Mehmonxona biznes sozlamalari (bazada, veb-paneldan o'zgartiriladi)."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.models import Setting, SystemState

DEFAULTS: dict[str, Any] = {
    "hotel_name": "Ramada",
    "hotel_phone": "+998 61 000 00 00",
    "entry_window_minutes": 30,  # FT-32: 10..120
    "discount_auto_limit_percent": 10,  # shundan kichik chegirmani administrator tasdiqlaydi
    "discount_timeout_minutes": 10,
    "checkin_hour": 14,
    "checkout_hour": 12,
    "early_checkin_half_from_hour": 6,  # 06:00–14:00 orasida kelish — 50%
    "late_checkout_half_until_hour": 18,  # 12:00–18:00 orasida chiqish — 50%, keyin — 100%
    "early_late_half_percent": 50,
    "wifi_ssid": "Hotel-Guest",
    "wifi_password": "",
    "breakfast_time": "07:00–10:00",
    "guest_incentive": "",  # masalan: «QR tasdiqlagan mehmonga 1 soat bepul kech chiqish»
    "qr_reminder_minutes": 60,  # FT-25
    "guest_link_days": 7,  # FT-20
    "morning_check_hour": 10,  # FT-40
    "daily_report_hour": 9,  # FT-43
    "nvr_offline_minutes": 15,  # FT-36
    "edge_unverifiable_hours": 6,  # shuncha soat edge yo'q bo'lsa — «tekshirib bo'lmadi»
    "checkout_grace_minutes": 15,  # chiqishdan keyin mehmon xonadan chiqib ketishi uchun
    "cleaning_grace_minutes": 5,
    "underpaid_grace_minutes": 15,  # R8: qisman to'lovdan keyin shuncha kutiladi
    "r11_threshold_pp": 15,  # R11: o'rtachadan shuncha foiz-punkt farq
    "r11_min_bookings": 5,
    # Kamera (2-bosqich va bronsiz faollik)
    "corroboration_minutes": 3,  # bitta qisqa hodisa shuncha vaqt ichida tasdiqlanmasa — o'rta daraja
    "repeat_window_seconds": 90,  # shu oraliqda bir eshikda 2+ hodisa — «kuchli» dalil
    "clock_skew_limit_seconds": 120,  # NVR soati shundan ko'p farq qilsa — edge vaqti ishlatiladi
    "camera_silence_hours": 24,  # kamera shuncha soat hodisa bermasa — R12
    "calibration_until": None,  # zona sozlash rejimi (ISO vaqt) — R2/R3 vaqtincha o'chadi
    "keyboard_check_enabled": True,  # FT-46
    "survey_enabled": True,
}


def get(db: Session, key: str) -> Any:
    row = db.get(Setting, key)
    if row is None:
        return DEFAULTS.get(key)
    return row.value


def get_int(db: Session, key: str) -> int:
    return int(get(db, key))


def set_value(db: Session, key: str, value: Any) -> tuple[Any, Any]:
    row = db.get(Setting, key)
    old = row.value if row else DEFAULTS.get(key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    db.flush()
    return old, value


def all_settings(db: Session) -> dict[str, Any]:
    out = dict(DEFAULTS)
    for row in db.query(Setting).all():
        out[row.key] = row.value
    return out


def state_get(db: Session, key: str, default: Any = None) -> Any:
    row = db.get(SystemState, key)
    return row.value if row else default


def state_set(db: Session, key: str, value: Any) -> None:
    row = db.get(SystemState, key)
    if row is None:
        db.add(SystemState(key=key, value=value))
    else:
        row.value = value
    db.flush()
