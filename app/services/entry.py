"""2-bosqich: kamera orqali kirish tasdig'i va bronsiz faollik (FT-30..FT-36, R1–R3, R12)."""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import clock
from app.config import get_settings
from app.enums import ConfirmSource, WindowResult, ZoneKind
from app.models import (
    Booking,
    Camera,
    CameraZone,
    CleaningSession,
    EntryWindow,
    Occupancy,
    Room,
    ZoneEvent,
)
from app.services import alerts, audit, bookings
from app.services import settings_svc as cfg

EDGE_HEARTBEAT_KEY = "edge_last_heartbeat"
EDGE_OFFLINE_SINCE_KEY = "edge_offline_alerted"


# ------------------------------------------------------------------ windows
def active_window(db: Session, booking: Booking) -> EntryWindow | None:
    return db.scalars(
        select(EntryWindow).where(EntryWindow.booking_id == booking.id, EntryWindow.result == WindowResult.OPEN)
    ).first()


def open_window(db: Session, booking: Booking, start: datetime | None = None) -> EntryWindow:
    supersede_windows(db, booking)
    start = start or clock.now()
    minutes = max(10, min(120, cfg.get_int(db, "entry_window_minutes")))
    w = EntryWindow(
        booking_id=booking.id,
        room_id=booking.room_id,
        opened_at=start,
        closes_at=start + timedelta(minutes=minutes),
        result=WindowResult.OPEN,
    )
    db.add(w)
    db.flush()
    audit.log(db, "system", "window.open", w, new={"room": booking.room.number, "minutes": minutes})
    # Oyna ochilishidan oldin (to'lov webhooki kechikkan) kelgan hodisalarni ham tekshiramiz
    early = db.scalars(
        select(ZoneEvent)
        .where(
            ZoneEvent.room_id == booking.room_id,
            ZoneEvent.kind == ZoneKind.DOOR,
            ZoneEvent.occurred_at >= start - timedelta(minutes=2),
            ZoneEvent.occurred_at <= w.closes_at,
        )
        .order_by(ZoneEvent.occurred_at)
    ).first()
    if early is not None:
        _confirm_entry(db, w, early)
    return w


def supersede_windows(db: Session, booking: Booking) -> None:
    for w in db.scalars(
        select(EntryWindow).where(EntryWindow.booking_id == booking.id, EntryWindow.result == WindowResult.OPEN)
    ):
        w.result = WindowResult.SUPERSEDED
        w.closed_at = clock.now()


def _confirm_entry(db: Session, w: EntryWindow, ev: ZoneEvent) -> None:
    w.result = WindowResult.ENTERED
    w.entered_event_id = ev.id
    w.closed_at = clock.now()
    ev.verdict = "entered"
    booking = w.booking
    bookings.add_confirmation(
        db,
        booking,
        2,
        ConfirmSource.CAMERA,
        {"zone_event_id": ev.id, "video": ev.video_link, "at": ev.occurred_at.isoformat(), "window_id": w.id},
    )
    audit.log(db, "camera", "window.entered", w, new={"event": ev.id})


# ------------------------------------------------------------------ lookups
def occupancy_at(db: Session, room_id: int, at: datetime) -> Occupancy | None:
    grace = timedelta(minutes=cfg.get_int(db, "checkout_grace_minutes"))
    rows = db.scalars(
        select(Occupancy).where(
            Occupancy.room_id == room_id,
            Occupancy.start_at <= at,
            or_(Occupancy.end_at.is_(None), Occupancy.end_at >= at - grace),
        )
    ).all()
    return rows[0] if rows else None


def cleaning_at(db: Session, room_id: int, at: datetime) -> CleaningSession | None:
    grace = timedelta(minutes=cfg.get_int(db, "cleaning_grace_minutes"))
    return db.scalars(
        select(CleaningSession).where(
            CleaningSession.room_id == room_id,
            CleaningSession.started_at <= at + grace,
            or_(CleaningSession.finished_at.is_(None), CleaningSession.finished_at >= at - grace),
        )
    ).first()


def windows_covering(db: Session, at: datetime) -> list[EntryWindow]:
    return list(
        db.scalars(
            select(EntryWindow).where(
                EntryWindow.opened_at <= at + timedelta(minutes=2),
                EntryWindow.closes_at >= at,
                EntryWindow.result.in_([WindowResult.OPEN, WindowResult.MISSED]),
            )
        )
    )


def video_link_for(event: ZoneEvent) -> str:
    return f"{get_settings().base_url.rstrip('/')}/panel/events/{event.id}"


# ------------------------------------------------------------------ ingest
def ingest_event(
    db: Session,
    *,
    channel: int,
    region_id: int,
    event_type: str,
    occurred_at: datetime,
    edge_event_id: str | None = None,
    snapshot_path: str | None = None,
    raw: dict | None = None,
) -> ZoneEvent | None:
    """NVR hodisasini qabul qilish (edge agent orqali). Takroriy hodisa — None."""
    if edge_event_id and db.scalar(select(ZoneEvent.id).where(ZoneEvent.edge_event_id == edge_event_id)):
        return None
    camera = db.scalar(select(Camera).where(Camera.channel == channel))
    zone = None
    if camera is not None:
        kind_pref = ZoneKind.ENTRANCE if event_type.lower() in ("linedetection", "linecrossing") else ZoneKind.DOOR
        zone = db.scalar(
            select(CameraZone).where(
                CameraZone.camera_id == camera.id, CameraZone.region_id == region_id, CameraZone.kind == kind_pref
            )
        ) or db.scalar(select(CameraZone).where(CameraZone.camera_id == camera.id, CameraZone.region_id == region_id))
    ev = ZoneEvent(
        zone_id=zone.id if zone else None,
        room_id=zone.room_id if zone and zone.kind == ZoneKind.DOOR else None,
        camera_id=camera.id if camera else None,
        kind=zone.kind if zone else "unmapped",
        event_type=event_type,
        occurred_at=clock.as_utc(occurred_at),
        received_at=clock.now(),
        edge_event_id=edge_event_id,
        snapshot_path=snapshot_path,
        raw=raw,
    )
    db.add(ev)
    db.flush()
    ev.video_link = video_link_for(ev)
    if ev.kind == ZoneKind.DOOR and ev.room_id is not None:
        process_door_event(db, ev)
    elif ev.kind == ZoneKind.ENTRANCE:
        ev.verdict = "entrance"
    else:
        ev.verdict = "unmapped"
    return ev


def process_door_event(db: Session, ev: ZoneEvent) -> None:
    at = ev.occurred_at
    room = db.get(Room, ev.room_id)
    # 1) Shu xona uchun ochiq kirish oynasi bo'lsa — 2/3
    for w in windows_covering(db, at):
        if w.room_id == room.id and w.result in (WindowResult.OPEN, WindowResult.MISSED):
            if w.booking.is_open and w.booking.room_id == room.id:
                was_missed = w.result == WindowResult.MISSED
                _confirm_entry(db, w, ev)
                if was_missed:
                    ev.verdict = "entered_late_event"
                return
    # 2) Xonada yashayotgan mehmon bor — normal
    if occupancy_at(db, room.id, at) is not None:
        ev.verdict = "occupied"
        return
    # 3) Farrosh ishlayapti — normal
    if cleaning_at(db, room.id, at) is not None:
        ev.verdict = "cleaning"
        return
    # 4) Bronsiz/bo'sh xona. Shu payt boshqa xonaga to'langan oyna ochiq bo'lsa — R3 (kritik)
    pending = db.scalars(
        select(Booking).where(Booking.room_id == room.id, Booking.status == "pending")
    ).first()
    other_windows = [w for w in windows_covering(db, at) if w.room_id != room.id and w.booking.is_open]
    if other_windows:
        w = other_windows[0]
        w.other_room_event_id = ev.id
        ev.verdict = "R3"
        alerts.raise_alert(
            db,
            "R3",
            f"Boshqa xona berilgan bo'lishi mumkin: bron #{w.booking_id} {w.room.number}-xona uchun to'langan, "
            f"lekin kirish oynasida bo'sh {room.number}-xona eshigida harakat qayd etildi.",
            room=room,
            booking=w.booking,
            zone_event=ev,
            dedupe_key=f"R3:{w.id}:{room.id}",
            data={"paid_room": w.room.number, "entered_room": room.number, "window_id": w.id},
            at=at,
        )
        return
    ev.verdict = "R2"
    extra = f" (to'lanmagan bron #{pending.id} bor)" if pending else ""
    alerts.raise_alert(
        db,
        "R2",
        f"Bronsiz/bo'sh {room.number}-xona eshigida harakat, farrosh belgisi yo'q{extra}.",
        room=room,
        booking=pending,
        zone_event=ev,
        dedupe_key=f"R2:{room.id}:{clock.local(at):%Y%m%d%H}",
        at=at,
    )


# ------------------------------------------------------------------ edge health
def heartbeat(db: Session, payload: dict | None = None) -> None:
    cfg.state_set(db, EDGE_HEARTBEAT_KEY, {"at": clock.now().isoformat(), **(payload or {})})
    if cfg.state_get(db, EDGE_OFFLINE_SINCE_KEY):
        cfg.state_set(db, EDGE_OFFLINE_SINCE_KEY, None)
        audit.log(db, "edge", "edge.online", object_type="edge", object_id="nvr")


def last_heartbeat(db: Session) -> datetime | None:
    v = cfg.state_get(db, EDGE_HEARTBEAT_KEY)
    if not v:
        return None
    return datetime.fromisoformat(v["at"])


def edge_status(db: Session) -> dict:
    v = cfg.state_get(db, EDGE_HEARTBEAT_KEY) or {}
    hb = last_heartbeat(db)
    limit = cfg.get_int(db, "nvr_offline_minutes")
    online = hb is not None and clock.now() - hb <= timedelta(minutes=limit)
    return {"last": hb, "online": online, "nvr_ok": v.get("nvr_ok", online), "queue": v.get("queue", 0)}


def check_edge(db: Session) -> None:
    """FT-36: NVR/edge bilan aloqa 15 daqiqadan ortiq uzilsa — ogohlantirish (R12)."""
    hb = last_heartbeat(db)
    limit = cfg.get_int(db, "nvr_offline_minutes")
    v = cfg.state_get(db, EDGE_HEARTBEAT_KEY) or {}
    nvr_down = v.get("nvr_ok") is False
    offline = hb is None or clock.now() - hb > timedelta(minutes=limit)
    if (offline or nvr_down) and not cfg.state_get(db, EDGE_OFFLINE_SINCE_KEY):
        since = hb.isoformat() if hb else None
        cfg.state_set(db, EDGE_OFFLINE_SINCE_KEY, since or clock.now().isoformat())
        what = "NVR javob bermayapti" if nvr_down and not offline else "edge/NVR bilan aloqa yo'q"
        alerts.raise_alert(
            db,
            "R12",
            f"Kamera tizimi: {what} (oxirgi aloqa: {clock.fmt(hb)}). 2-bosqich vaqtincha tekshirilmaydi.",
            dedupe_key=f"R12:edge:{since or clock.now().isoformat()}",
            data={"kind": "nvr"},
        )
        # Egaga darhol ham xabar (FT-36): o'rta daraja bo'lsa ham
        from app.services import notify

        for o in notify.owners(db):
            notify.later(db, o.telegram_id, f"🟡 R12: {what}. Oxirgi aloqa: {clock.fmt(hb)}")


# ------------------------------------------------------------------ sweep
def sweep_windows(db: Session) -> None:
    """Muddati o'tgan kirish oynalarini yopish (R1) yoki «tekshirib bo'lmadi» deb belgilash."""
    now = clock.now()
    hb = last_heartbeat(db)
    unverifiable_after = timedelta(hours=cfg.get_int(db, "edge_unverifiable_hours"))
    limit = timedelta(minutes=cfg.get_int(db, "nvr_offline_minutes"))
    v = cfg.state_get(db, EDGE_HEARTBEAT_KEY) or {}
    nvr_ok = v.get("nvr_ok", True)
    for w in db.scalars(
        select(EntryWindow).where(EntryWindow.result == WindowResult.OPEN, EntryWindow.closes_at < now)
    ).all():
        booking = w.booking
        if not booking.is_open:
            w.result = WindowResult.SUPERSEDED
            w.closed_at = now
            continue
        # Edge oyna yopilgandan keyin aloqada bo'lgan bo'lsa — barcha hodisalar yetib kelgan
        if hb is not None and hb >= w.closes_at and nvr_ok:
            w.result = WindowResult.MISSED
            w.closed_at = now
            audit.log(db, "system", "window.missed", w)
            if w.other_room_event_id is None:
                alerts.raise_alert(
                    db,
                    "R1",
                    f"Bron #{booking.id}: to'lov bor, lekin kirish oynasida {w.room.number}-xona eshigida harakat yo'q.",
                    booking=booking,
                    room=w.room,
                    dedupe_key=f"R1:{w.id}",
                )
            continue
        edge_online = hb is not None and now - hb <= limit
        if edge_online and not nvr_ok:
            # Edge ishlayapti, lekin NVR o'zi ishlamayapti — hodisalar umuman yo'q
            mark_unverifiable(db, w)
        elif not edge_online and now - w.closes_at > unverifiable_after:
            # Internet/edge uzoq vaqt yo'q — 2-bosqich «tekshirib bo'lmadi»
            mark_unverifiable(db, w)
        # aks holda: edge qayta ulanib, NVR'da saqlangan hodisalarni yuborishini kutamiz


def mark_unverifiable(db: Session, w: EntryWindow) -> None:
    w.result = WindowResult.UNVERIFIABLE
    w.closed_at = clock.now()
    bookings.add_confirmation(
        db, w.booking, 2, ConfirmSource.CAMERA_UNVERIFIABLE, {"window_id": w.id, "reason": "nvr_offline"}
    )
    audit.log(db, "system", "window.unverifiable", w)
    from app.services import notify

    for o in notify.owners(db):
        notify.later(
            db,
            o.telegram_id,
            f"🟡 Bron #{w.booking_id} ({w.room.number}): kamera ishlamagani uchun 2-bosqich «tekshirib bo'lmadi». "
            f"3-bosqich va farrosh tasdig'i bilan davom etiladi.",
        )


def entrance_count(db: Session, start: datetime, end: datetime) -> int:
    return len(
        db.scalars(
            select(ZoneEvent.id).where(
                ZoneEvent.kind == ZoneKind.ENTRANCE, ZoneEvent.occurred_at >= start, ZoneEvent.occurred_at < end
            )
        ).all()
    )
