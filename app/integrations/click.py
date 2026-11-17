"""Click SHOP API (Prepare / Complete).

Hujjat: https://docs.click.uz/click-api-request/ — so'rovlar form-urlencoded,
imzo: md5(click_trans_id + service_id + SECRET_KEY + merchant_trans_id
[+ merchant_prepare_id] + amount + action + sign_time).
`merchant_trans_id` = bron ID.
"""
from __future__ import annotations

import hashlib
import hmac
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import BookingStatus, PaymentMethod
from app.models import Booking, ClickTransaction
from app.services import audit, bookings

OK = 0
E_SIGN = -1
E_AMOUNT = -2
E_ACTION = -3
E_ALREADY_PAID = -4
E_NOT_FOUND = -5
E_TX_NOT_FOUND = -6
E_UPDATE = -7
E_REQUEST = -8
E_CANCELLED = -9


def checkout_url(booking: Booking, amount_sum: int, return_url: str) -> str:
    s = get_settings()
    q = urlencode(
        {
            "service_id": s.click_service_id,
            "merchant_id": s.click_merchant_id,
            "amount": amount_sum,
            "transaction_param": booking.id,
            "return_url": return_url,
        }
    )
    return f"{s.click_checkout_url}?{q}"


def _sign(*parts) -> str:
    return hashlib.md5("".join(str(p) for p in parts).encode()).hexdigest()


def _resp(f: dict, error: int, note: str, **extra) -> dict:
    out = {
        "click_trans_id": f.get("click_trans_id"),
        "merchant_trans_id": f.get("merchant_trans_id"),
        "error": error,
        "error_note": note,
    }
    out.update(extra)
    return out


def _amount_ok(amount_str: str, due: int) -> bool:
    try:
        return abs(float(amount_str) - due) < 0.01
    except (TypeError, ValueError):
        return False


def prepare(db: Session, f: dict) -> dict:
    secret = get_settings().click_secret_key
    expected = _sign(
        f.get("click_trans_id"), f.get("service_id"), secret, f.get("merchant_trans_id"),
        f.get("amount"), f.get("action"), f.get("sign_time"),
    )
    if not secret or not hmac.compare_digest(expected, f.get("sign_string", "")):
        return _resp(f, E_SIGN, "SIGN CHECK FAILED!")
    if str(f.get("action")) != "0":
        return _resp(f, E_ACTION, "Action not found")
    try:
        b = db.get(Booking, int(f.get("merchant_trans_id")))
    except (TypeError, ValueError):
        b = None
    if b is None or not b.is_open:
        return _resp(f, E_NOT_FOUND, "Booking does not exist")
    if not b.smartphoneless and b.status == BookingStatus.PENDING and b.guest_accepted_at is None:
        return _resp(f, E_NOT_FOUND, "Booking is not confirmed by guest")
    due = bookings.amount_due_now(db, b)
    if due <= 0:
        return _resp(f, E_ALREADY_PAID, "Already paid")
    if not _amount_ok(f.get("amount"), due):
        return _resp(f, E_AMOUNT, "Incorrect parameter amount")
    tx = db.scalar(select(ClickTransaction).where(ClickTransaction.click_trans_id == str(f.get("click_trans_id"))))
    if tx is None:
        tx = ClickTransaction(
            click_trans_id=str(f.get("click_trans_id")),
            click_paydoc_id=str(f.get("click_paydoc_id") or ""),
            merchant_trans_id=str(f.get("merchant_trans_id")),
            booking_id=b.id,
            amount=due,
            status="prepared",
        )
        db.add(tx)
        db.flush()
        audit.log(db, "click", "click.prepare", tx, new={"booking": b.id, "amount": due})
    return _resp(f, OK, "Success", merchant_prepare_id=tx.id)


def complete(db: Session, f: dict) -> dict:
    secret = get_settings().click_secret_key
    expected = _sign(
        f.get("click_trans_id"), f.get("service_id"), secret, f.get("merchant_trans_id"),
        f.get("merchant_prepare_id"), f.get("amount"), f.get("action"), f.get("sign_time"),
    )
    if not secret or not hmac.compare_digest(expected, f.get("sign_string", "")):
        return _resp(f, E_SIGN, "SIGN CHECK FAILED!")
    if str(f.get("action")) != "1":
        return _resp(f, E_ACTION, "Action not found")
    try:
        tx = db.get(ClickTransaction, int(f.get("merchant_prepare_id")))
    except (TypeError, ValueError):
        tx = None
    if tx is None or tx.click_trans_id != str(f.get("click_trans_id")):
        return _resp(f, E_TX_NOT_FOUND, "Transaction does not exist")
    if tx.status == "paid":
        return _resp(f, E_ALREADY_PAID, "Already paid", merchant_confirm_id=tx.payment_id)
    if tx.status == "cancelled":
        return _resp(f, E_CANCELLED, "Transaction cancelled")
    try:
        click_error = int(f.get("error", 0))
    except (TypeError, ValueError):
        click_error = 0
    if click_error < 0:
        tx.status = "cancelled"
        audit.log(db, "click", "click.cancel", tx, new={"error": click_error})
        return _resp(f, E_CANCELLED, "Transaction cancelled")
    if not _amount_ok(f.get("amount"), tx.amount):
        return _resp(f, E_AMOUNT, "Incorrect parameter amount")
    b = db.get(Booking, tx.booking_id)
    try:
        p = bookings.record_payment(
            db, b, PaymentMethod.CLICK, tx.amount, external_id=f"click:{tx.click_trans_id}", actor="click"
        )
    except bookings.BookingError:
        return _resp(f, E_UPDATE, "Failed to update booking")
    tx.status = "paid"
    tx.payment_id = p.id
    return _resp(f, OK, "Success", merchant_confirm_id=p.id)
