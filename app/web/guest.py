"""Mehmon sahifasi (SMS havolasi) va xona QR sahifasi. Ilova/ro'yxatdan o'tish talab qilinmaydi."""
from __future__ import annotations

import time
from collections import defaultdict

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.enums import BookingStatus, QrAnswer
from app.i18n import LANGS, pick_lang
from app.security import device_hash, new_device_id, sign_payload, unsign_payload
from app.services import bookings, guestflow
from app.services import settings_svc as cfg
from app.web.common import templates

router = APIRouter()

DEVICE_COOKIE = "sq_dev"
LANG_COOKIE = "sq_lang"
_unlock_attempts: dict[str, list[float]] = defaultdict(list)


def _device(request: Request) -> tuple[str, bool]:
    dev = request.cookies.get(DEVICE_COOKIE)
    if dev and 16 <= len(dev) <= 64:
        return dev, False
    return new_device_id(), True


def _lang(request: Request) -> str:
    return pick_lang(request.headers.get("accept-language"), request.query_params.get("lang") or request.cookies.get(LANG_COOKIE))


def _finish(request: Request, resp: Response, dev: str, new_dev: bool) -> Response:
    secure = get_settings().base_url.startswith("https")
    if new_dev:
        resp.set_cookie(DEVICE_COOKIE, dev, max_age=60 * 60 * 24 * 60, httponly=True, samesite="lax", secure=secure)
    q = request.query_params.get("lang")
    if q in LANGS:
        resp.set_cookie(LANG_COOKIE, q, max_age=60 * 60 * 24 * 60, samesite="lax", secure=secure)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


def _hotel(db: Session) -> dict:
    return {
        "name": cfg.get(db, "hotel_name"),
        "phone": cfg.get(db, "hotel_phone"),
    }


def _unlocked(request: Request, booking) -> bool:
    data = unsign_payload(request.cookies.get(f"sq_ul_{booking.id}"), "guest-unlock")
    return bool(data and data.get("b") == booking.id)


def _render(request: Request, name: str, ctx: dict, status: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(request, name, ctx, status_code=status)


# ------------------------------------------------------------------ booking page
@router.get("/g/{token}", response_class=HTMLResponse)
def guest_page(token: str, request: Request, db: Session = Depends(get_db)):
    dev, new_dev = _device(request)
    lang = _lang(request)
    b = guestflow.booking_by_token(db, token)
    ctx = {"lang": lang, "hotel": _hotel(db), "path": request.url.path}
    if b is None:
        return _finish(request, _render(request, "guest/message.html", ctx | {"msg_key": "g.expired"}, 404), dev, new_dev)
    if b.guest.lang != lang and request.query_params.get("lang"):
        b.guest.lang = lang
    dh = device_hash(dev)
    if b.status in (BookingStatus.PENDING, BookingStatus.ACTIVE) and not guestflow.link_valid(b):
        return _finish(request, _render(request, "guest/message.html", ctx | {"msg_key": "g.expired"}), dev, new_dev)
    if not guestflow.device_allowed(b, dh) and not _unlocked(request, b):
        return _finish(request, _render(request, "guest/unlock.html", ctx | {"b": b, "wrong": False}), dev, new_dev)
    am = bookings.get_pending_amendment(db, b)
    due_now = bookings.amount_due_now(db, b)
    survey_done = b.status == BookingStatus.CHECKED_OUT and _has_survey(db, b)
    resp = _render(
        request,
        "guest/booking.html",
        ctx
        | {
            "b": b,
            "am": am,
            "due_now": due_now,
            "survey_done": survey_done,
            "survey_enabled": cfg.get(db, "survey_enabled"),
            "wifi": {"ssid": cfg.get(db, "wifi_ssid"), "password": cfg.get(db, "wifi_password")},
            "breakfast": cfg.get(db, "breakfast_time"),
            "incentive": cfg.get(db, "guest_incentive"),
        },
    )
    return _finish(request, resp, dev, new_dev)


def _has_survey(db: Session, b) -> bool:
    from sqlalchemy import select

    from app.models import Survey

    return db.scalar(select(Survey.id).where(Survey.booking_id == b.id)) is not None


def _guarded(request: Request, db: Session, token: str):
    b = guestflow.booking_by_token(db, token)
    if b is None or not guestflow.link_valid(b) and b.is_open:
        return None, None
    dev, _ = _device(request)
    dh = device_hash(dev)
    if not guestflow.device_allowed(b, dh) and not _unlocked(request, b):
        return None, None
    return b, dh


@router.post("/g/{token}/accept")
def guest_accept(token: str, request: Request, db: Session = Depends(get_db)):
    dev, new_dev = _device(request)
    b, dh = _guarded(request, db, token)
    if b is not None and b.status == BookingStatus.PENDING:
        try:
            bookings.guest_accept(db, b, device_hash(dev))
        except bookings.BookingError:
            pass
    return _finish(request, RedirectResponse(f"/g/{token}", status_code=303), dev, new_dev)


@router.post("/g/{token}/unlock")
def guest_unlock(token: str, request: Request, last4: str = Form(""), db: Session = Depends(get_db)):
    dev, new_dev = _device(request)
    lang = _lang(request)
    b = guestflow.booking_by_token(db, token)
    if b is None:
        return RedirectResponse(f"/g/{token}", status_code=303)
    key = f"{b.id}:{request.client.host if request.client else ''}"
    now = time.time()
    _unlock_attempts[key] = [x for x in _unlock_attempts[key] if now - x < 3600]
    if len(_unlock_attempts[key]) >= guestflow.MAX_LAST4_ATTEMPTS:
        ctx = {"lang": lang, "hotel": _hotel(db), "msg_key": "g.too_many", "path": f"/g/{token}"}
        return _render(request, "guest/message.html", ctx, 429)
    if guestflow.phone_last4_ok(b, last4.strip()):
        resp = RedirectResponse(f"/g/{token}", status_code=303)
        resp.set_cookie(
            f"sq_ul_{b.id}", sign_payload({"b": b.id}, "guest-unlock", 86400), max_age=86400, httponly=True, samesite="lax"
        )
        return _finish(request, resp, dev, new_dev)
    _unlock_attempts[key].append(now)
    ctx = {"lang": lang, "hotel": _hotel(db), "b": b, "wrong": True, "path": f"/g/{token}"}
    return _finish(request, _render(request, "guest/unlock.html", ctx, 400), dev, new_dev)


@router.post("/g/{token}/amendment")
def guest_accept_amendment(token: str, request: Request, db: Session = Depends(get_db)):
    b, _ = _guarded(request, db, token)
    if b is not None:
        am = bookings.get_pending_amendment(db, b)
        if am is not None:
            bookings.guest_accept_amendment(db, b, am)
    return RedirectResponse(f"/g/{token}", status_code=303)


@router.post("/g/{token}/survey")
def guest_survey(
    token: str,
    request: Request,
    rating: int | None = Form(None),
    complaint: str | None = Form(None),
    paid_claim: str | None = Form(None),
    comment: str | None = Form(None),
    db: Session = Depends(get_db),
):
    b, _ = _guarded(request, db, token)
    if b is not None and b.status == BookingStatus.CHECKED_OUT:
        claim = _parse_amount(paid_claim)
        guestflow.submit_survey(db, b, rating, bool(complaint), claim, comment)
    return RedirectResponse(f"/g/{token}?survey=1", status_code=303)


def _parse_amount(v: str | None) -> int | None:
    if not v:
        return None
    digits = "".join(ch for ch in v if ch.isdigit())
    return int(digits) if digits and len(digits) < 12 else None


# ------------------------------------------------------------------ room QR (3-bosqich)
def _scan_page(request: Request, db: Session, token: str, last4: str | None):
    dev, new_dev = _device(request)
    lang = _lang(request)
    ip = request.client.host if request.client else None
    res = guestflow.scan(db, token, device_hash(dev), ip, last4=last4)
    ctx = {
        "lang": lang,
        "hotel": _hotel(db),
        "res": res,
        "token": token,
        "path": request.url.path,
        "wifi": {"ssid": cfg.get(db, "wifi_ssid"), "password": cfg.get(db, "wifi_password")},
        "breakfast": cfg.get(db, "breakfast_time"),
        "incentive": cfg.get(db, "guest_incentive"),
    }
    status = 200 if res.kind != "bad_code" else 404
    return _finish(request, _render(request, "guest/qr.html", ctx, status), dev, new_dev)


@router.get("/q/{token}", response_class=HTMLResponse)
def qr_scan(token: str, request: Request, db: Session = Depends(get_db)):
    return _scan_page(request, db, token, None)


@router.post("/q/{token}", response_class=HTMLResponse)
def qr_last4(token: str, request: Request, last4: str = Form(""), db: Session = Depends(get_db)):
    return _scan_page(request, db, token, last4.strip())


@router.post("/q/{token}/answer/{scan_id}", response_class=HTMLResponse)
def qr_answer(
    token: str,
    scan_id: int,
    request: Request,
    answer: str = Form(...),
    claimed_amount: str | None = Form(None),
    claimed_room: str | None = Form(None),
    db: Session = Depends(get_db),
):
    dev, new_dev = _device(request)
    lang = _lang(request)
    if answer not in {a.value for a in QrAnswer}:
        return RedirectResponse(f"/q/{token}", status_code=303)
    b = guestflow.answer(
        db, scan_id, answer, device_hash(dev), _parse_amount(claimed_amount), (claimed_room or "").strip()[:10] or None
    )
    ctx = {
        "lang": lang,
        "hotel": _hotel(db),
        "token": token,
        "path": request.url.path,
        "answered": answer,
        "b": b,
        "wifi": {"ssid": cfg.get(db, "wifi_ssid"), "password": cfg.get(db, "wifi_password")},
        "breakfast": cfg.get(db, "breakfast_time"),
        "incentive": cfg.get(db, "guest_incentive"),
    }
    if b is None:
        return RedirectResponse(f"/q/{token}", status_code=303)
    return _finish(request, _render(request, "guest/qr_done.html", ctx), dev, new_dev)
