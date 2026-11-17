"""Ega va administrator veb-paneli."""
from __future__ import annotations

import hashlib
import hmac
import io
from datetime import date, timedelta
from pathlib import Path

import qrcode
import qrcode.image.svg
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.config import get_settings
from app.db import get_db
from app.enums import AlertStatus, BookingStatus, DayType, Role, RoomState, ZoneKind
from app.models import (
    Alert,
    AuditLog,
    Booking,
    Camera,
    CameraZone,
    CancelRequest,
    Discount,
    EntryWindow,
    Holiday,
    QrScan,
    Room,
    RoomCategory,
    RoomCheck,
    Season,
    Shift,
    Survey,
    Tariff,
    User,
    ZoneEvent,
)
from app.security import (
    hash_password,
    make_room_token,
    new_invite_code,
    sign_payload,
    unsign_payload,
    verify_password,
)
from app.services import alerts as alerts_svc
from app.services import audit, bookings, entry, export, reports
from app.services import settings_svc as cfg
from app.web.common import templates

router = APIRouter(prefix="/panel")
COOKIE = "sq_panel"


class NeedLogin(Exception):
    pass


# ------------------------------------------------------------------ auth
def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    data = unsign_payload(request.cookies.get(COOKIE), "panel")
    if not data:
        raise NeedLogin()
    user = db.get(User, data.get("u"))
    if user is None or not user.active or user.role not in (Role.OWNER, Role.ADMIN):
        raise NeedLogin()
    request.state.user = user
    request.state.csrf = _csrf(request.cookies.get(COOKIE))
    return user


def owner_only(user: User = Depends(current_user)) -> User:
    if user.role != Role.OWNER:
        raise HTTPException(403, "Faqat ega uchun")
    return user


def _csrf(session_token: str | None) -> str:
    key = hashlib.sha256(("csrf:" + get_settings().secret_key).encode()).digest()
    return hmac.new(key, (session_token or "").encode(), hashlib.sha256).hexdigest()[:32]


async def check_csrf(request: Request):
    if request.method == "POST":
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf", "")), _csrf(request.cookies.get(COOKIE))):
            raise HTTPException(400, "CSRF token noto'g'ri — sahifani yangilang")


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("user", getattr(request.state, "user", None))
    ctx.setdefault("csrf", getattr(request.state, "csrf", ""))
    ctx.setdefault("edge", None)
    return templates.TemplateResponse(request, f"panel/{name}", ctx)


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "panel/login.html", {"error": None, "user": None})


@router.post("/login")
def login(request: Request, login: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.login == login.strip(), User.active.is_(True)))
    if user is None or user.role not in (Role.OWNER, Role.ADMIN) or not verify_password(password, user.password_hash):
        audit.log(db, "anon", "panel.login_failed", object_type="users", object_id=login[:40])
        return templates.TemplateResponse(
            request, "panel/login.html", {"error": "Login yoki parol noto'g'ri", "user": None}, status_code=401
        )
    audit.log(db, audit.actor_of(user), "panel.login", user)
    resp = back("/panel/")
    resp.set_cookie(
        COOKIE,
        sign_payload({"u": user.id}, "panel", 60 * 60 * 12),
        max_age=60 * 60 * 12,
        httponly=True,
        samesite="strict",
        secure=get_settings().base_url.startswith("https"),
    )
    return resp


@router.get("/logout")
def logout():
    resp = back("/panel/login")
    resp.delete_cookie(COOKIE)
    return resp


# ------------------------------------------------------------------ dashboard
@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rooms = db.scalars(select(Room).where(Room.active.is_(True)).order_by(Room.floor, Room.number)).all()
    open_b = {b.room_id: b for b in bookings.open_bookings(db) if b.status == BookingStatus.ACTIVE}
    pending = {b.room_id: b for b in bookings.open_bookings(db) if b.status == BookingStatus.PENDING}
    floors: dict[int, list] = {}
    for r in rooms:
        floors.setdefault(r.floor, []).append({"room": r, "b": open_b.get(r.id), "p": pending.get(r.id)})
    open_alerts = db.scalars(
        select(Alert).where(Alert.status == AlertStatus.NEW).order_by(Alert.at.desc()).limit(15)
    ).all()
    today = clock.today()
    d = reports.daily(db, today)
    return render(
        request,
        "dashboard.html",
        floors=floors,
        alerts=open_alerts,
        d=d,
        edge=entry.edge_status(db),
        pending_discounts=db.scalars(select(Discount).where(Discount.status == "pending")).all(),
        pending_cancels=db.scalars(select(CancelRequest).where(CancelRequest.status == "pending")).all(),
    )


# ------------------------------------------------------------------ bookings
@router.get("/bookings", response_class=HTMLResponse)
def bookings_list(
    request: Request,
    status: str | None = None,
    q: str | None = None,
    start: str | None = None,
    end: str | None = None,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    stmt = select(Booking).order_by(Booking.id.desc())
    if status:
        stmt = stmt.where(Booking.status == status)
    if start:
        stmt = stmt.where(Booking.created_at >= clock.local_dt(date.fromisoformat(start)))
    if end:
        stmt = stmt.where(Booking.created_at < clock.local_dt(date.fromisoformat(end) + timedelta(days=1)))
    rows = db.scalars(stmt.limit(300)).all()
    if q:
        ql = q.lower().strip()
        rows = [
            b for b in rows
            if ql in b.guest.full_name.lower() or ql in b.guest.phone or ql == b.room.number or ql == str(b.id)
        ]
    return render(request, "bookings.html", rows=rows, status=status, q=q or "", start=start or "", end=end or "")


@router.get("/bookings/{bid}", response_class=HTMLResponse)
def booking_detail(bid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = db.get(Booking, bid)
    if b is None:
        raise HTTPException(404)
    windows = db.scalars(select(EntryWindow).where(EntryWindow.booking_id == bid).order_by(EntryWindow.id)).all()
    scans = db.scalars(select(QrScan).where(QrScan.found_booking_id == bid).order_by(QrScan.id)).all()
    al = db.scalars(select(Alert).where(Alert.booking_id == bid).order_by(Alert.id)).all()
    logs = db.scalars(
        select(AuditLog)
        .where(AuditLog.object_type == "bookings", AuditLog.object_id == str(bid))
        .order_by(AuditLog.id)
    ).all()
    discounts = db.scalars(select(Discount).where(Discount.booking_id == bid)).all()
    survey = db.scalar(select(Survey).where(Survey.booking_id == bid))
    return render(
        request, "booking.html", b=b, windows=windows, scans=scans, alerts=al, logs=logs, discounts=discounts,
        survey=survey, guest_url=f"{get_settings().base_url.rstrip('/')}/g/{b.guest_token}",
    )


@router.post("/bookings/{bid}/cancel", dependencies=[Depends(check_csrf)])
def booking_cancel(bid: int, reason: str = Form(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = db.get(Booking, bid)
    bookings.cancel(db, b, user, reason)
    return back(f"/panel/bookings/{bid}")


@router.post("/bookings/{bid}/resend", dependencies=[Depends(check_csrf)])
def booking_resend(bid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = db.get(Booking, bid)
    if b and b.is_open:
        bookings.send_guest_link(db, b)
        audit.log(db, audit.actor_of(user), "booking.sms_resend", b)
    return back(f"/panel/bookings/{bid}")


@router.post("/bookings/{bid}/checkout", dependencies=[Depends(check_csrf)])
def booking_checkout(bid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = db.get(Booking, bid)
    bookings.checkout(db, b, user, force=True)
    return back(f"/panel/bookings/{bid}")


@router.get("/guest-photo/{bid}")
def guest_photo(bid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    b = db.get(Booking, bid)
    if b is None or not b.guest.document_photo:
        raise HTTPException(404)
    path = Path(b.guest.document_photo).resolve()
    root = Path(get_settings().storage_dir).resolve()
    if root not in path.parents or not path.exists():
        raise HTTPException(404)
    audit.log(db, audit.actor_of(user), "guest.photo_view", b.guest)
    return FileResponse(path, headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ alerts
@router.get("/alerts", response_class=HTMLResponse)
def alerts_list(
    request: Request, status: str | None = "new", rule: str | None = None,
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    stmt = select(Alert).order_by(Alert.at.desc())
    if status:
        stmt = stmt.where(Alert.status == status)
    if rule:
        stmt = stmt.where(Alert.rule == rule)
    rows = db.scalars(stmt.limit(300)).all()
    if user.role == Role.ADMIN:
        # Administrator o'z smenasidagi ogohlantirishlarni ko'radi (TT 5-bo'lim)
        my_shifts = set(db.scalars(select(Shift.id).where(Shift.user_id == user.id)))
        rows = [a for a in rows if a.shift_id in my_shifts or a.user_id == user.id]
    return render(request, "alerts.html", rows=rows, status=status or "", rule=rule or "", rules=alerts_svc.RULES)


@router.post("/alerts/{aid}", dependencies=[Depends(check_csrf)])
def alert_update(
    aid: int, status: str = Form(...), note: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)
):
    a = db.get(Alert, aid)
    if a is None or status not in {s.value for s in AlertStatus}:
        raise HTTPException(400)
    alerts_svc.set_status(db, a, status, user, note or None)
    return back("/panel/alerts")


@router.get("/events/{eid}", response_class=HTMLResponse)
def event_view(eid: int, request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = db.get(ZoneEvent, eid)
    if ev is None:
        raise HTTPException(404)
    cam = db.get(Camera, ev.camera_id) if ev.camera_id else None
    room = db.get(Room, ev.room_id) if ev.room_id else None
    t0 = ev.occurred_at - timedelta(seconds=20)
    t1 = ev.occurred_at + timedelta(seconds=40)
    rtsp = None
    if cam is not None:
        rtsp = (
            f"rtsp://NVR_IP:554/Streaming/tracks/{cam.channel}01?"
            f"starttime={t0:%Y%m%dT%H%M%SZ}&endtime={t1:%Y%m%dT%H%M%SZ}"
        )
    audit.log(db, audit.actor_of(user), "event.view", ev)
    return render(request, "event.html", ev=ev, cam=cam, room=room, rtsp=rtsp)


@router.get("/events/{eid}/snapshot")
def event_snapshot(eid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ev = db.get(ZoneEvent, eid)
    if ev is None or not ev.snapshot_path:
        raise HTTPException(404)
    path = Path(ev.snapshot_path).resolve()
    if Path(get_settings().storage_dir).resolve() not in path.parents or not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/jpeg")


@router.get("/events", response_class=HTMLResponse)
def events_list(request: Request, room: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(ZoneEvent).order_by(ZoneEvent.occurred_at.desc())
    if room:
        r = db.scalar(select(Room).where(Room.number == room))
        if r:
            stmt = stmt.where(ZoneEvent.room_id == r.id)
    rows = db.scalars(stmt.limit(300)).all()
    rooms = {r.id: r.number for r in db.scalars(select(Room))}
    return render(request, "events.html", rows=rows, rooms=rooms, room=room or "", edge=entry.edge_status(db))


# ------------------------------------------------------------------ rooms & cameras
@router.get("/rooms", response_class=HTMLResponse)
def rooms_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rooms = db.scalars(select(Room).order_by(Room.floor, Room.number)).all()
    cats = db.scalars(select(RoomCategory).order_by(RoomCategory.id)).all()
    cams = db.scalars(select(Camera).order_by(Camera.channel)).all()
    zones = db.scalars(select(CameraZone)).all()
    zone_by_room: dict[int, list] = {}
    for z in zones:
        if z.room_id:
            zone_by_room.setdefault(z.room_id, []).append(z)
    entrance = [z for z in zones if z.kind == ZoneKind.ENTRANCE]
    last_qr = dict(
        db.execute(
            select(RoomCheck.room_id, func.max(RoomCheck.day)).where(RoomCheck.kind == "qr_plate").group_by(RoomCheck.room_id)
        ).all()
    )
    return render(
        request, "rooms.html", rooms=rooms, cats=cats, cams=cams, zone_by_room=zone_by_room, entrance=entrance,
        last_qr=last_qr, states=[s.value for s in RoomState],
    )


@router.post("/rooms/{rid:int}", dependencies=[Depends(check_csrf)])
def room_update(
    rid: int, category_id: int = Form(...), state: str = Form(...), active: str | None = Form(None),
    user: User = Depends(owner_only), db: Session = Depends(get_db),
):
    r = db.get(Room, rid)
    old = {"category": r.category_id, "state": r.state, "active": r.active}
    r.category_id, r.state, r.active = category_id, state, bool(active)
    audit.log(db, audit.actor_of(user), "room.update", r, old=old, new={"category": category_id, "state": state, "active": bool(active)})
    return back("/panel/rooms")


@router.post("/rooms/add", dependencies=[Depends(check_csrf)])
def room_add(
    number: str = Form(...), floor: int = Form(...), category_id: int = Form(...),
    user: User = Depends(owner_only), db: Session = Depends(get_db),
):
    r = Room(number=number.strip(), floor=floor, category_id=category_id)
    db.add(r)
    db.flush()
    audit.log(db, audit.actor_of(user), "room.add", r, new={"number": r.number})
    return back("/panel/rooms")


@router.post("/rooms/{rid:int}/reissue-qr", dependencies=[Depends(check_csrf)])
def room_reissue_qr(rid: int, user: User = Depends(owner_only), db: Session = Depends(get_db)):
    r = db.get(Room, rid)
    old = r.qr_version
    r.qr_version += 1
    audit.log(db, audit.actor_of(user), "room.qr_reissue", r, old={"v": old}, new={"v": r.qr_version})
    return back("/panel/rooms")


@router.post("/zones", dependencies=[Depends(check_csrf)])
def zone_save(
    camera_id: int = Form(...), region_id: int = Form(...), kind: str = Form("door"), room_id: str = Form(""),
    user: User = Depends(owner_only), db: Session = Depends(get_db),
):
    """FT-30: zona ↔ xona bog'lanishi (faqat ega)."""
    rid = int(room_id) if room_id else None
    z = db.scalar(
        select(CameraZone).where(CameraZone.camera_id == camera_id, CameraZone.region_id == region_id, CameraZone.kind == kind)
    )
    old = {"room": z.room_id} if z else None
    if z is None:
        z = CameraZone(camera_id=camera_id, region_id=region_id, kind=kind, room_id=rid)
        db.add(z)
    else:
        z.room_id = rid
    db.flush()
    audit.log(db, audit.actor_of(user), "zone.save", z, old=old, new={"camera": camera_id, "region": region_id, "kind": kind, "room": rid})
    return back("/panel/rooms")


@router.post("/zones/{zid}/delete", dependencies=[Depends(check_csrf)])
def zone_delete(zid: int, user: User = Depends(owner_only), db: Session = Depends(get_db)):
    z = db.get(CameraZone, zid)
    if z:
        audit.log(db, audit.actor_of(user), "zone.delete", z, old={"camera": z.camera_id, "region": z.region_id, "room": z.room_id})
        db.delete(z)
    return back("/panel/rooms")


@router.post("/cameras", dependencies=[Depends(check_csrf)])
def camera_save(
    name: str = Form(...), channel: int = Form(...), floor: str = Form(""),
    user: User = Depends(owner_only), db: Session = Depends(get_db),
):
    cam = db.scalar(select(Camera).where(Camera.channel == channel))
    if cam is None:
        cam = Camera(name=name, channel=channel, floor=int(floor) if floor else None)
        db.add(cam)
    else:
        cam.name, cam.floor = name, int(floor) if floor else None
    db.flush()
    audit.log(db, audit.actor_of(user), "camera.save", cam, new={"name": name, "channel": channel})
    return back("/panel/rooms")


@router.get("/rooms/qr-print", response_class=HTMLResponse)
def qr_print(request: Request, user: User = Depends(owner_only), db: Session = Depends(get_db)):
    rooms = db.scalars(select(Room).where(Room.active.is_(True)).order_by(Room.floor, Room.number)).all()
    base = get_settings().base_url.rstrip("/")
    items = []
    for r in rooms:
        url = f"{base}/q/{make_room_token(r.id, r.qr_version)}"
        img = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=1)
        buf = io.BytesIO()
        img.save(buf)
        svg = buf.getvalue().decode()
        svg = svg[svg.find("<svg"):]
        items.append({"room": r, "svg": svg, "url": url})
    audit.log(db, audit.actor_of(user), "room.qr_print", object_type="rooms", object_id="all")
    return render(request, "qr_print.html", items=items, hotel=cfg.get(db, "hotel_name"))


# ------------------------------------------------------------------ tariffs
@router.get("/tariffs", response_class=HTMLResponse)
def tariffs_page(request: Request, user: User = Depends(owner_only), db: Session = Depends(get_db)):
    cats = db.scalars(select(RoomCategory).order_by(RoomCategory.id)).all()
    seasons = db.scalars(select(Season).order_by(Season.start_date)).all()
    holidays = db.scalars(select(Holiday).order_by(Holiday.day)).all()
    tariffs = {(t.category_id, t.day_type, t.season_id): t.price for t in db.scalars(select(Tariff))}
    return render(
        request, "tariffs.html", cats=cats, seasons=seasons, holidays=holidays, tariffs=tariffs,
        day_types=[d.value for d in DayType],
    )


@router.post("/tariffs", dependencies=[Depends(check_csrf)])
async def tariffs_save(request: Request, user: User = Depends(owner_only), db: Session = Depends(get_db)):
    """FT-10: faqat ega o'zgartiradi, har bir o'zgarish jurnalga yoziladi."""
    form = await request.form()
    for key, value in form.items():
        if not key.startswith("p:"):
            continue
        _, cat_id, day_type, season = key.split(":")
        season_id = int(season) if season != "base" else None
        value = str(value).replace(" ", "").strip()
        row = db.scalar(
            select(Tariff).where(
                Tariff.category_id == int(cat_id),
                Tariff.day_type == day_type,
                Tariff.season_id.is_(None) if season_id is None else Tariff.season_id == season_id,
            )
        )
        if not value:
            if row is not None:
                audit.log(db, audit.actor_of(user), "tariff.delete", row, old={"price": row.price})
                db.delete(row)
            continue
        price = int(value)
        if row is None:
            row = Tariff(category_id=int(cat_id), day_type=day_type, season_id=season_id, price=price)
            db.add(row)
            db.flush()
            audit.log(db, audit.actor_of(user), "tariff.create", row, new={"price": price, "cat": cat_id, "day": day_type, "season": season_id})
        elif row.price != price:
            audit.log(db, audit.actor_of(user), "tariff.update", row, old={"price": row.price}, new={"price": price})
            row.price = price
    return back("/panel/tariffs")


@router.post("/categories", dependencies=[Depends(check_csrf)])
def category_add(code: str = Form(...), name: str = Form(...), capacity: int = Form(2),
                 user: User = Depends(owner_only), db: Session = Depends(get_db)):
    c = RoomCategory(code=code.strip(), name=name.strip(), capacity=capacity)
    db.add(c)
    db.flush()
    audit.log(db, audit.actor_of(user), "category.add", c, new={"code": code, "name": name})
    return back("/panel/tariffs")


@router.post("/seasons", dependencies=[Depends(check_csrf)])
def season_add(name: str = Form(...), start_date: date = Form(...), end_date: date = Form(...), priority: int = Form(0),
               user: User = Depends(owner_only), db: Session = Depends(get_db)):
    s = Season(name=name, start_date=start_date, end_date=end_date, priority=priority)
    db.add(s)
    db.flush()
    audit.log(db, audit.actor_of(user), "season.add", s, new={"name": name, "start": start_date, "end": end_date})
    return back("/panel/tariffs")


@router.post("/seasons/{sid}/delete", dependencies=[Depends(check_csrf)])
def season_delete(sid: int, user: User = Depends(owner_only), db: Session = Depends(get_db)):
    s = db.get(Season, sid)
    if s:
        for t_ in db.scalars(select(Tariff).where(Tariff.season_id == sid)):
            db.delete(t_)
        audit.log(db, audit.actor_of(user), "season.delete", s, old={"name": s.name})
        db.delete(s)
    return back("/panel/tariffs")


@router.post("/holidays", dependencies=[Depends(check_csrf)])
def holiday_add(day: date = Form(...), name: str = Form(...), user: User = Depends(owner_only), db: Session = Depends(get_db)):
    h = Holiday(day=day, name=name)
    db.add(h)
    db.flush()
    audit.log(db, audit.actor_of(user), "holiday.add", h, new={"day": day, "name": name})
    return back("/panel/tariffs")


@router.post("/holidays/{hid}/delete", dependencies=[Depends(check_csrf)])
def holiday_delete(hid: int, user: User = Depends(owner_only), db: Session = Depends(get_db)):
    h = db.get(Holiday, hid)
    if h:
        audit.log(db, audit.actor_of(user), "holiday.delete", h, old={"day": h.day})
        db.delete(h)
    return back("/panel/tariffs")


# ------------------------------------------------------------------ settings
SETTING_FIELDS = [
    ("hotel_name", "Mehmonxona nomi", "str"),
    ("hotel_phone", "Aloqa telefoni (mehmon sahifasida)", "str"),
    ("entry_window_minutes", "Kirish oynasi, daqiqa (10–120)", "int"),
    ("discount_auto_limit_percent", "Administrator tasdiqlaydigan chegirma chegarasi, %", "int"),
    ("discount_timeout_minutes", "Chegirma javobi muddati, daqiqa", "int"),
    ("checkin_hour", "Kirish soati", "int"),
    ("checkout_hour", "Chiqish soati", "int"),
    ("early_checkin_half_from_hour", "Erta kirish 50% shu soatdan (oldin — 100%)", "int"),
    ("late_checkout_half_until_hour", "Kech chiqish 50% shu soatgacha (keyin — 100%)", "int"),
    ("early_late_half_percent", "Erta/kech qo'shimcha haq, %", "int"),
    ("wifi_ssid", "Mehmon Wi-Fi nomi", "str"),
    ("wifi_password", "Mehmon Wi-Fi paroli (faqat 3-bosqichdan keyin)", "str"),
    ("breakfast_time", "Nonushta vaqti", "str"),
    ("guest_incentive", "QR tasdig'i uchun rag'bat (ixtiyoriy)", "str"),
    ("qr_reminder_minutes", "QR eslatma SMS, daqiqa", "int"),
    ("guest_link_days", "Mehmon havolasi amal qilish muddati, kun", "int"),
    ("morning_check_hour", "Farrosh tekshiruvi muddati, soat", "int"),
    ("daily_report_hour", "Kunlik hisobot soati", "int"),
    ("nvr_offline_minutes", "NVR aloqasi uzilishi chegarasi, daqiqa", "int"),
    ("checkout_grace_minutes", "Chiqishdan keyingi bag'rikenglik, daqiqa", "int"),
    ("underpaid_grace_minutes", "Kam to'lov (R8) kutish, daqiqa", "int"),
    ("r11_threshold_pp", "R11: o'rtachadan farq, foiz-punkt", "int"),
    ("keyboard_check_enabled", "Kalit taxtasi foto-tekshiruvi", "bool"),
    ("survey_enabled", "Chiqishda so'rovnoma", "bool"),
]


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, user: User = Depends(owner_only), db: Session = Depends(get_db)):
    return render(request, "settings.html", fields=SETTING_FIELDS, values=cfg.all_settings(db))


@router.post("/settings", dependencies=[Depends(check_csrf)])
async def settings_save(request: Request, user: User = Depends(owner_only), db: Session = Depends(get_db)):
    form = await request.form()
    for key, _label, kind in SETTING_FIELDS:
        if kind == "bool":
            value = form.get(key) == "on"
        else:
            raw = str(form.get(key, "")).strip()
            if kind == "int":
                try:
                    value = int(raw)
                except ValueError:
                    continue
                if key == "entry_window_minutes":
                    value = max(10, min(120, value))
            else:
                value = raw
        if cfg.get(db, key) != value:
            old, new = cfg.set_value(db, key, value)
            shown_old = "***" if key == "wifi_password" else old
            shown_new = "***" if key == "wifi_password" else new
            audit.log(db, audit.actor_of(user), "setting.update", object_type="settings", object_id=key,
                      old={"value": shown_old}, new={"value": shown_new})
    return back("/panel/settings")


# ------------------------------------------------------------------ staff
@router.get("/staff", response_class=HTMLResponse)
def staff_page(request: Request, user: User = Depends(owner_only), db: Session = Depends(get_db)):
    rows = db.scalars(select(User).order_by(User.active.desc(), User.role, User.full_name)).all()
    return render(request, "staff.html", rows=rows, roles=[r.value for r in Role], bot_hint=get_settings().telegram_bot_token != "")


@router.post("/staff", dependencies=[Depends(check_csrf)])
def staff_add(
    full_name: str = Form(...), role: str = Form(...), phone: str = Form(""), lang: str = Form("uz"),
    user: User = Depends(owner_only), db: Session = Depends(get_db),
):
    if role not in {r.value for r in Role}:
        raise HTTPException(400)
    u = User(full_name=full_name.strip(), role=role, phone=phone or None, lang=lang, invite_code=new_invite_code())
    db.add(u)
    db.flush()
    audit.log(db, audit.actor_of(user), "user.create", u, new={"name": u.full_name, "role": role})
    return back("/panel/staff")


@router.post("/staff/{uid}", dependencies=[Depends(check_csrf)])
def staff_update(
    uid: int, action: str = Form(...), login: str = Form(""), password: str = Form(""),
    user: User = Depends(owner_only), db: Session = Depends(get_db),
):
    u = db.get(User, uid)
    if u is None:
        raise HTTPException(404)
    if action == "deactivate" and u.id != user.id:
        u.active = False
        u.telegram_id = None
    elif action == "activate":
        u.active = True
    elif action == "invite":
        u.invite_code = new_invite_code()
        u.telegram_id = None
    elif action == "password" and u.role in (Role.OWNER, Role.ADMIN) and len(password) >= 8 and login.strip():
        u.login = login.strip()
        u.password_hash = hash_password(password)
    else:
        raise HTTPException(400, "Amal bajarilmadi (parol kamida 8 belgi, faqat ega/administrator uchun)")
    audit.log(db, audit.actor_of(user), f"user.{action}", u)
    return back("/panel/staff")


# ------------------------------------------------------------------ shifts, housekeeping, audit
@router.get("/shifts", response_class=HTMLResponse)
def shifts_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    rows = db.scalars(select(Shift).order_by(Shift.id.desc()).limit(200)).all()
    return render(request, "shifts.html", rows=rows)


@router.get("/housekeeping", response_class=HTMLResponse)
def housekeeping_page(request: Request, day: str | None = None, user: User = Depends(current_user), db: Session = Depends(get_db)):
    from app.services import housekeeping

    d = date.fromisoformat(day) if day else clock.today()
    checks = {c.room_id: c for c in db.scalars(select(RoomCheck).where(RoomCheck.day == d, RoomCheck.kind == "morning"))}
    rows = []
    for r in housekeeping.rooms(db):
        rows.append({"room": r, "check": checks.get(r.id), "booking": housekeeping.occupied_overnight(db, r.id, d)})
    users = {u.id: u.full_name for u in db.scalars(select(User))}
    return render(request, "housekeeping.html", rows=rows, day=d, users=users)


@router.get("/audit", response_class=HTMLResponse)
def audit_page(request: Request, action: str | None = None, user: User = Depends(owner_only), db: Session = Depends(get_db)):
    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if action:
        stmt = stmt.where(AuditLog.action.like(f"{action}%"))
    rows = db.scalars(stmt.limit(500)).all()
    return render(request, "audit.html", rows=rows, action=action or "")


# ------------------------------------------------------------------ reports
@router.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request, kind: str = "daily", day: str | None = None,
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    d = date.fromisoformat(day) if day else None
    if kind == "weekly":
        if d is not None:
            d = d - timedelta(days=d.weekday())
        data = reports.weekly(db, d)
    elif kind == "monthly":
        data = reports.monthly(db, d.replace(day=1) if d else None)
    else:
        kind = "daily"
        data = reports.daily(db, d)
    return render(request, "reports.html", kind=kind, data=data, day=day or "")


@router.get("/export.xlsx")
def export_xlsx(start: str, end: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    s, e = date.fromisoformat(start), date.fromisoformat(end) + timedelta(days=1)
    data = export.build_xlsx(db, s, e)
    audit.log(db, audit.actor_of(user), "export.xlsx", object_type="export", object_id=f"{start}..{end}")
    return Response(
        data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="shaffof_{start}_{end}.xlsx"'},
    )

