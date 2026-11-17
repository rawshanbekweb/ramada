"""Payme Merchant API (JSON-RPC 2.0).

Hujjat: https://developer.help.paycom.uz/ — CheckPerformTransaction,
CreateTransaction, PerformTransaction, CancelTransaction, CheckTransaction,
GetStatement. Hisob parametri: `account.booking_id`. Summalar tiyinda.
"""
from __future__ import annotations

import base64
import hmac

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import BookingStatus, PaymentMethod
from app.models import Booking, Payment, PaymeTransaction
from app.services import audit, bookings

TIMEOUT_MS = 43_200_000  # 12 soat

E_AUTH = -32504
E_METHOD = -32601
E_PARSE = -32700
E_AMOUNT = -31001
E_TX_NOT_FOUND = -31003
E_CANT_CANCEL = -31007
E_CANT_PERFORM = -31008
E_ACCOUNT = -31050  # -31050..-31099: hisob xatolari


def _msg(uz: str, ru: str, en: str) -> dict:
    return {"uz": uz, "ru": ru, "en": en}


class PaymeError(Exception):
    def __init__(self, code: int, message: dict, data: str | None = None):
        self.code, self.message, self.data = code, message, data


def now_ms() -> int:
    from app import clock

    return int(clock.now().timestamp() * 1000)


def check_auth(header: str | None) -> bool:
    key = get_settings().payme_key
    if not header or not header.startswith("Basic ") or not key:
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode()
    except (ValueError, UnicodeDecodeError):
        return False
    login, _, password = decoded.partition(":")
    return login == "Paycom" and hmac.compare_digest(password, key)


def checkout_url(booking: Booking, amount_sum: int, return_url: str) -> str:
    s = get_settings()
    params = f"m={s.payme_merchant_id};ac.booking_id={booking.id};a={amount_sum * 100};c={return_url}"
    return f"{s.payme_checkout_url.rstrip('/')}/{base64.b64encode(params.encode()).decode()}"


def _booking_from_account(db: Session, account: dict | None) -> Booking:
    try:
        bid = int((account or {}).get("booking_id"))
    except (TypeError, ValueError):
        raise PaymeError(E_ACCOUNT, _msg("Bron topilmadi", "Бронь не найдена", "Booking not found"), "booking_id")
    b = db.get(Booking, bid)
    if b is None:
        raise PaymeError(E_ACCOUNT, _msg("Bron topilmadi", "Бронь не найдена", "Booking not found"), "booking_id")
    if not b.is_open:
        raise PaymeError(E_ACCOUNT - 1, _msg("Bron yopilgan", "Бронь закрыта", "Booking is closed"), "booking_id")
    return b


def _validate_amount(db: Session, b: Booking, amount: int) -> None:
    due = bookings.amount_due_now(db, b)
    if due <= 0:
        raise PaymeError(E_ACCOUNT - 2, _msg("To'lov talab qilinmaydi", "Оплата не требуется", "Nothing to pay"), "booking_id")
    if not b.smartphoneless and b.status == BookingStatus.PENDING and b.guest_accepted_at is None:
        raise PaymeError(
            E_ACCOUNT - 3,
            _msg("Avval bronni tasdiqlang", "Сначала подтвердите бронь", "Confirm the booking first"),
            "booking_id",
        )
    if amount != due * 100:
        raise PaymeError(E_AMOUNT, _msg("Summa noto'g'ri", "Неверная сумма", "Incorrect amount"))


def _tx(db: Session, paycom_id: str) -> PaymeTransaction:
    tx = db.scalar(select(PaymeTransaction).where(PaymeTransaction.paycom_id == paycom_id))
    if tx is None:
        raise PaymeError(E_TX_NOT_FOUND, _msg("Tranzaksiya topilmadi", "Транзакция не найдена", "Transaction not found"))
    return tx


def _cancel_timed_out(db: Session, tx: PaymeTransaction) -> None:
    tx.state = -1
    tx.reason = 4
    tx.cancel_time = now_ms()


def handle(db: Session, method: str, params: dict) -> dict:
    if method == "CheckPerformTransaction":
        b = _booking_from_account(db, params.get("account"))
        _validate_amount(db, b, int(params.get("amount", 0)))
        return {"allow": True}

    if method == "CreateTransaction":
        tx = db.scalar(select(PaymeTransaction).where(PaymeTransaction.paycom_id == params["id"]))
        if tx is not None:
            if tx.state != 1:
                raise PaymeError(E_CANT_PERFORM, _msg("Amalni bajarib bo'lmaydi", "Невозможно выполнить", "Cannot perform"))
            if now_ms() - tx.create_time > TIMEOUT_MS:
                _cancel_timed_out(db, tx)
                raise PaymeError(E_CANT_PERFORM, _msg("Muddat o'tgan", "Истёк срок", "Timed out"))
            return {"create_time": tx.create_time, "transaction": str(tx.id), "state": tx.state}
        b = _booking_from_account(db, params.get("account"))
        _validate_amount(db, b, int(params["amount"]))
        busy = db.scalar(
            select(PaymeTransaction).where(PaymeTransaction.booking_id == b.id, PaymeTransaction.state == 1)
        )
        if busy is not None:
            if now_ms() - busy.create_time > TIMEOUT_MS:
                _cancel_timed_out(db, busy)
            else:
                raise PaymeError(E_ACCOUNT - 9, _msg("Bron to'lov kutmoqda", "Бронь ожидает оплаты", "Pending payment exists"), "booking_id")
        tx = PaymeTransaction(
            paycom_id=params["id"],
            paycom_time=int(params["time"]),
            amount=int(params["amount"]),
            booking_id=b.id,
            state=1,
            create_time=now_ms(),
        )
        db.add(tx)
        db.flush()
        audit.log(db, "payme", "payme.create", tx, new={"booking": b.id, "amount": tx.amount})
        return {"create_time": tx.create_time, "transaction": str(tx.id), "state": 1}

    if method == "PerformTransaction":
        tx = _tx(db, params["id"])
        if tx.state == 2:
            return {"transaction": str(tx.id), "perform_time": tx.perform_time, "state": 2}
        if tx.state != 1:
            raise PaymeError(E_CANT_PERFORM, _msg("Amalni bajarib bo'lmaydi", "Невозможно выполнить", "Cannot perform"))
        if now_ms() - tx.create_time > TIMEOUT_MS:
            _cancel_timed_out(db, tx)
            raise PaymeError(E_CANT_PERFORM, _msg("Muddat o'tgan", "Истёк срок", "Timed out"))
        b = db.get(Booking, tx.booking_id)
        p = bookings.record_payment(
            db, b, PaymentMethod.PAYME, tx.amount // 100, external_id=f"payme:{tx.paycom_id}", actor="payme"
        )
        tx.state = 2
        tx.perform_time = now_ms()
        tx.payment_id = p.id
        return {"transaction": str(tx.id), "perform_time": tx.perform_time, "state": 2}

    if method == "CancelTransaction":
        tx = _tx(db, params["id"])
        reason = int(params.get("reason", 0) or 0)
        if tx.state == 1:
            tx.state = -1
        elif tx.state == 2:
            # To'langan pul qaytarilmoqda — to'lovni bekor qilamiz va egaga xabar beramiz
            tx.state = -2
            if tx.payment_id:
                p = db.get(Payment, tx.payment_id)
                p.status = "cancelled"
                from app.services import alerts

                b = db.get(Booking, tx.booking_id)
                alerts.raise_alert(
                    db,
                    "R10",
                    f"Payme to'lovi qaytarildi: bron #{b.id} ({b.room.number}), {tx.amount // 100:,} so'm. Sabab kodi {reason}.",
                    booking=b,
                    dedupe_key=f"R10:payme:{tx.paycom_id}",
                )
        else:
            return {"transaction": str(tx.id), "cancel_time": tx.cancel_time, "state": tx.state}
        tx.reason = reason
        tx.cancel_time = now_ms()
        audit.log(db, "payme", "payme.cancel", tx, new={"state": tx.state, "reason": reason})
        return {"transaction": str(tx.id), "cancel_time": tx.cancel_time, "state": tx.state}

    if method == "CheckTransaction":
        tx = _tx(db, params["id"])
        return {
            "create_time": tx.create_time,
            "perform_time": tx.perform_time,
            "cancel_time": tx.cancel_time,
            "transaction": str(tx.id),
            "state": tx.state,
            "reason": tx.reason,
        }

    if method == "GetStatement":
        rows = db.scalars(
            select(PaymeTransaction)
            .where(PaymeTransaction.paycom_time >= int(params["from"]), PaymeTransaction.paycom_time <= int(params["to"]))
            .order_by(PaymeTransaction.paycom_time)
        ).all()
        return {
            "transactions": [
                {
                    "id": tx.paycom_id,
                    "time": tx.paycom_time,
                    "amount": tx.amount,
                    "account": {"booking_id": str(tx.booking_id)},
                    "create_time": tx.create_time,
                    "perform_time": tx.perform_time,
                    "cancel_time": tx.cancel_time,
                    "transaction": str(tx.id),
                    "state": tx.state,
                    "reason": tx.reason,
                }
                for tx in rows
            ]
        }

    raise PaymeError(E_METHOD, _msg("Metod topilmadi", "Метод не найден", "Method not found"))


def rpc(db: Session, auth_header: str | None, body: dict) -> dict:
    req_id = body.get("id")
    if not check_auth(auth_header):
        return {"id": req_id, "error": {"code": E_AUTH, "message": _msg("Ruxsat yo'q", "Нет доступа", "Unauthorized")}}
    try:
        result = handle(db, body.get("method", ""), body.get("params") or {})
        return {"id": req_id, "result": result}
    except PaymeError as e:
        err = {"code": e.code, "message": e.message}
        if e.data:
            err["data"] = e.data
        return {"id": req_id, "error": err}
    except bookings.BookingError as e:
        db.rollback()
        return {"id": req_id, "error": {"code": E_CANT_PERFORM, "message": _msg(str(e), str(e), str(e))}}
    except (KeyError, ValueError, TypeError):
        db.rollback()
        return {"id": req_id, "error": {"code": E_PARSE, "message": _msg("So'rov xato", "Ошибка запроса", "Bad request")}}
