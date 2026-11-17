"""Excel eksport (FT-45): bronlar, to'lovlar, tasdiqlar, ogohlantirishlar."""
from __future__ import annotations

import io
from datetime import date

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.models import Alert, Booking, Confirmation, Payment


def _dt(v):
    return clock.local(v).replace(tzinfo=None) if v else None


def _sheet(wb: Workbook, title: str, header: list[str], rows: list[list]):
    ws = wb.create_sheet(title)
    ws.append(header)
    for c in ws[1]:
        c.font = Font(bold=True)
    for r in rows:
        ws.append(r)
    for i, h in enumerate(header, 1):
        width = max([len(str(h))] + [len(str(r[i - 1])) for r in rows[:500] if r[i - 1] is not None]) + 2
        ws.column_dimensions[get_column_letter(i)].width = min(width, 50)
    ws.freeze_panes = "A2"


def build_xlsx(db: Session, start: date, end: date) -> bytes:
    s, e = clock.local_dt(start), clock.local_dt(end)
    wb = Workbook()
    wb.remove(wb.active)
    bks = db.scalars(select(Booking).where(Booking.created_at >= s, Booking.created_at < e).order_by(Booking.id)).all()
    _sheet(
        wb,
        "Bronlar",
        ["ID", "Yaratildi", "Xona", "Toifa", "Kirish", "Chiqish", "Tunlar", "Mehmon", "Telefon", "Rasmiy", "Chegirma",
         "Qo'shimcha", "Yakuniy", "To'langan", "Holat", "Tasdiq", "Smartfonsiz", "Xodim", "1/3", "2/3", "3/3"],
        [
            [b.id, _dt(b.created_at), b.room.number, b.room.category.name, b.checkin_date, b.checkout_date, b.nights,
             b.guest.full_name, b.guest.phone, b.official_amount, b.discount_amount, b.extra_amount, b.final_amount,
             b.paid_amount, b.status, f"{b.stage}/3", "ha" if b.smartphoneless else "", b.created_by.full_name,
             _dt(b.stage1_at), _dt(b.stage2_at), _dt(b.stage3_at)]
            for b in bks
        ],
    )
    pays = db.scalars(select(Payment).where(Payment.created_at >= s, Payment.created_at < e).order_by(Payment.id)).all()
    _sheet(
        wb,
        "To'lovlar",
        ["ID", "Vaqt", "Bron", "Xona", "Usul", "Summa", "Holat", "Tashqi ID", "Chek", "Smena"],
        [
            [p.id, _dt(p.paid_at or p.created_at), p.booking_id, p.booking.room.number, p.method, p.amount, p.status,
             p.external_id, p.receipt_no, p.shift_id]
            for p in pays
        ],
    )
    confs = db.scalars(
        select(Confirmation).where(Confirmation.at >= s, Confirmation.at < e).order_by(Confirmation.id)
    ).all()
    _sheet(
        wb,
        "Tasdiqlar",
        ["ID", "Vaqt", "Bron", "Bosqich", "Manba", "Bekor", "Dalil"],
        [[c.id, _dt(c.at), c.booking_id, c.stage, c.source, "ha" if c.void else "", str(c.evidence)] for c in confs],
    )
    als = db.scalars(select(Alert).where(Alert.at >= s, Alert.at < e).order_by(Alert.id)).all()
    _sheet(
        wb,
        "Ogohlantirishlar",
        ["ID", "Vaqt", "Qoida", "Daraja", "Xona", "Bron", "Xodim", "Xabar", "Holat", "Izoh", "Video"],
        [
            [a.id, _dt(a.at), a.rule, a.severity, a.room.number if a.room else None, a.booking_id,
             a.user.full_name if a.user else None, a.message, a.status, a.note, a.video_link]
            for a in als
        ],
    )
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
