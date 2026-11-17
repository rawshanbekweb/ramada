"""2-bosqich: kamera orqali kirish tasdig'i va bronsiz faollik (FT-30..FT-36, R1–R3, R12).

Kamera mantig'ining asosiy tamoyillari:

* **Dalil kuchi.** «Bostirib kirish» (intrusion / fielddetection — odam eshik oldida
  belgilangan soniyadan ko'p turdi) yoki bir eshikda qisqa vaqt ichida 2+ hodisa —
  *kuchli* dalil. Bitta qisqa «hududga kirish» (yonidan o'tib ketgan odam) — *kuchsiz*.
  Kuchli dalil darhol R2/R3 beradi; kuchsiz dalil `corroboration_minutes` ichida
  tasdiqlanmasa, faqat past darajada (hisobotda) qayd etiladi (TT 20-bo'lim:
  «yuqori daraja faqat boshqa dalil bilan mos kelganda»).
* **Kuzatuv uzilishlari.** NVR oqimi uzilgan, kanal oflayn, kamera yopilgan davrlar
  `CoverageGap` sifatida saqlanadi. Shu davrga to'g'ri kelgan kirish oynasi uchun R1
  berilmaydi — 2-bosqich «tekshirib bo'lmadi» deb belgilanadi.
* **To'liq sinxronlash.** Oyna «kirish yo'q» (R1) deb faqat edge navbati bo'shab,
  oyna yopilgan vaqtdan keyingi barcha hodisalar yetib kelganidan so'ng yopiladi.
* **Kamera sog'lig'i.** Kamera yopilishi / signal yo'qolishi / kanal oflayn bo'lishi —
  darhol egaga (R12, yuqori daraja). Uzoq vaqt hodisa bermagan kamera ham R12.
* **NVR soati.** NVR vaqti edge (NTP) vaqtidan sezilarli farq qilsa, edge vaqti olinadi.
* **Sozlash rejimi.** Audit paytida eshiklarni bittalab sinash uchun R2/R3 vaqtincha
  o'chiriladi (ko'pi bilan 4 soat), bog'lanmagan zonalar panelda ko'rinadi.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import clock
from app.config import get_settings
from app.enums import ConfirmSource, WindowResult, ZoneKind
from app.models import (
    Alert,
    Booking,
    Camera,
    CameraZone,
    CleaningSession,
    CoverageGap,
    EntryWindow,
    Occupancy,
    Room,
    ZoneEvent,
)
from app.services import alerts, audit, bookings
from app.services import settings_svc as cfg

EDGE_HEARTBEAT_KEY = "edge_last_heartbeat"
EDGE_OFFLINE_SINCE_KEY = "edge_offline_alerted"
EDGE_SYNCED_KEY = "edge_synced_at"  # edge navbati oxirgi marta bo'sh bo'lgan vaqt

# Kanonik turlar (edge yuboradi) va eski Hikvision nomlari
STRONG_TYPES = {"dwell", "fielddetection", "intrusion", "wanderdetection", "loitering"}
LINE_TYPES = {"line", "linedetection", "linecrossing", "crosslinedetection"}
HEALTH_TYPES = {"videoloss", "shelteralarm", "tamper", "videotampering", "videoblind"}
HUMAN_TARGETS = {"human", "person", "people", "pedestrian"}


# ------------------------------------------------------------------ helpers
def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(v) -> datetime | None:
    if not v:
        return None
    try:
        return clock.as_utc(datetime.fromisoformat(v))
    except (TypeError, ValueError):
        return None


def calibration_active(db: Session) -> bool:
    until = _parse_dt(cfg.get(db, "calibration_until"))
    return until is not None and until > clock.now()


def room_channels(db: Session, room_id: int) -> set[int]:
    return set(
        db.scalars(
            select(Camera.channel)
            .join(CameraZone, CameraZone.camera_id == Camera.id)
            .where(CameraZone.room_id == room_id, CameraZone.kind == ZoneKind.DOOR)
        )
    )


def video_link_for(event: ZoneEvent) -> str:
    return f"{get_settings().base_url.rstrip('/')}/panel/events/{event.id}"


def _tell_owners(db: Session, text: str) -> None:
    from app.services import notify

    for o in notify.owners(db):
        notify.later(db, o.telegram_id, text)


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
    # To'lov yozilishidan biroz oldin kelgan hodisa ham hisobga olinadi
    early = db.scalars(
        select(ZoneEvent)
        .where(
            ZoneEvent.room_id == booking.room_id,
            ZoneEvent.kind == ZoneKind.DOOR,
            ZoneEvent.occurred_at >= start - timedelta(minutes=2),
            ZoneEvent.occurred_at <= w.closes_at,
            or_(ZoneEvent.verdict.is_(None), ZoneEvent.verdict != "non_human"),
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
    bookings.add_confirmation(
        db,
        w.booking,
        2,
        ConfirmSource.CAMERA,
        {
            "zone_event_id": ev.id,
            "video": ev.video_link,
            "at": ev.occurred_at.isoformat(),
            "window_id": w.id,
            "strength": ev.strength,
        },
    )
    audit.log(db, "camera", "window.entered", w, new={"event": ev.id})


# ------------------------------------------------------------------ lookups
def occupancy_at(db: Session, room_id: int, at: datetime) -> Occupancy | None:
    grace = timedelta(minutes=cfg.get_int(db, "checkout_grace_minutes"))
    return db.scalars(
        select(Occupancy).where(
            Occupancy.room_id == room_id,
            Occupancy.start_at <= at,
            or_(Occupancy.end_at.is_(None), Occupancy.end_at >= at - grace),
        )
    ).first()


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
                EntryWindow.result.in_([WindowResult.OPEN, WindowResult.MISSED, WindowResult.UNVERIFIABLE]),
            )
        )
    )


def is_strong(db: Session, ev: ZoneEvent) -> bool:
    """Kuchli dalil: intrusion (odam eshik oldida turdi) yoki shu eshikda qisqa vaqtda takroriy hodisa."""
    if ev.event_type.lower() in STRONG_TYPES:
        return True
    window = timedelta(seconds=cfg.get_int(db, "repeat_window_seconds"))
    prior = db.scalar(
        select(func.count(ZoneEvent.id)).where(
            ZoneEvent.room_id == ev.room_id,
            ZoneEvent.kind == ZoneKind.DOOR,
            ZoneEvent.id != ev.id,
            ZoneEvent.occurred_at >= ev.occurred_at - window,
            ZoneEvent.occurred_at <= ev.occurred_at + window,
            or_(ZoneEvent.verdict.is_(None), ZoneEvent.verdict != "non_human"),
        )
    )
    return bool(prior)


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
    target: str | None = None,
    state: str | None = None,
    edge_received_at: datetime | None = None,
) -> ZoneEvent | None:
    """NVR hodisasini qabul qilish (edge agent orqali). Takroriy hodisa — None."""
    if edge_event_id and db.scalar(select(ZoneEvent.id).where(ZoneEvent.edge_event_id == edge_event_id)):
        return None
    occurred_at = clock.as_utc(occurred_at)
    skew = None
    if edge_received_at is not None:
        edge_received_at = clock.as_utc(edge_received_at)
        skew = int((occurred_at - edge_received_at).total_seconds())
        if abs(skew) > cfg.get_int(db, "clock_skew_limit_seconds"):
            # NVR soati adashgan: edge (NTP) vaqtiga ishonamiz
            occurred_at = edge_received_at
            alerts.raise_alert(
                db,
                "R12",
                f"NVR soati {abs(skew) // 60} daqiqa {'oldinda' if skew > 0 else 'orqada'}. "
                f"Hodisalar edge vaqti bo'yicha hisoblanmoqda; NVR'da NTP sozlang.",
                dedupe_key=f"R12:clock:{clock.today().isoformat()}",
                data={"kind": "clock", "skew_s": skew},
            )
    etype = event_type.lower()
    camera = db.scalar(select(Camera).where(Camera.channel == channel))
    if camera is not None and etype not in HEALTH_TYPES:
        if camera.last_event_at is None or camera.last_event_at < occurred_at:
            camera.last_event_at = occurred_at

    if etype in HEALTH_TYPES:
        ev = ZoneEvent(
            camera_id=camera.id if camera else None, kind="health", event_type=event_type,
            occurred_at=occurred_at, received_at=clock.now(), edge_event_id=edge_event_id,
            snapshot_path=snapshot_path, raw=raw, channel=channel, region_id=region_id,
            state=(state or "active").lower(), skew_s=skew,
        )
        db.add(ev)
        db.flush()
        ev.video_link = video_link_for(ev)
        process_health_event(db, ev, camera)
        return ev

    zone = None
    if camera is not None:
        kind_pref = ZoneKind.ENTRANCE if etype in LINE_TYPES else ZoneKind.DOOR
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
        occurred_at=occurred_at,
        received_at=clock.now(),
        edge_event_id=edge_event_id,
        snapshot_path=snapshot_path,
        raw=raw,
        channel=channel,
        region_id=region_id,
        target=(target or None) and target.lower(),
        skew_s=skew,
    )
    db.add(ev)
    db.flush()
    ev.video_link = video_link_for(ev)
    if ev.target and ev.target not in HUMAN_TARGETS:
        ev.verdict = "non_human"
    elif ev.kind == ZoneKind.DOOR and ev.room_id is not None:
        process_door_event(db, ev)
    elif ev.kind == ZoneKind.ENTRANCE:
        ev.verdict = "entrance"
    else:
        ev.verdict = "unmapped"
    return ev


def process_door_event(db: Session, ev: ZoneEvent) -> None:
    at = ev.occurred_at
    room = db.get(Room, ev.room_id)
    ev.strength = "strong" if is_strong(db, ev) else "weak"
    # 1) Shu xona uchun ochiq kirish oynasi bo'lsa — 2/3 (kuchsiz dalil ham yetadi)
    for w in windows_covering(db, at):
        if w.room_id == room.id and w.booking.is_open and w.booking.room_id == room.id and w.result != WindowResult.ENTERED:
            if w.booking.stage < 2:
                was = w.result
                _confirm_entry(db, w, ev)
                if was != WindowResult.OPEN:
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
    # 4) Zona sozlash rejimi — ogohlantirishsiz
    if calibration_active(db):
        ev.verdict = "calibration"
        return
    # 5) Bronsiz/bo'sh xona
    other_windows = [w for w in windows_covering(db, at) if w.room_id != room.id and w.booking.is_open]
    if other_windows:
        w = other_windows[0]
        w.other_room_event_id = ev.id
        if ev.strength == "strong":
            _raise_r3(db, ev, room, w, weak=False)
        else:
            ev.verdict = "R3?"
        return
    if ev.strength == "strong":
        _raise_r2(db, ev, room, weak=False)
    else:
        ev.verdict = "R2?"


def _mark_corroborated(db: Session, ev: ZoneEvent) -> None:
    window = timedelta(seconds=cfg.get_int(db, "repeat_window_seconds"))
    for prev in db.scalars(
        select(ZoneEvent).where(
            ZoneEvent.room_id == ev.room_id,
            ZoneEvent.verdict.in_(["R2?", "R3?"]),
            ZoneEvent.occurred_at >= ev.occurred_at - window,
            ZoneEvent.id != ev.id,
        )
    ):
        prev.verdict = "corroborated"


def _raise_r2(db: Session, ev: ZoneEvent, room: Room, weak: bool) -> None:
    pending = db.scalars(select(Booking).where(Booking.room_id == room.id, Booking.status == "pending")).first()
    hour = f"{clock.local(ev.occurred_at):%Y%m%d%H}"
    extra = f" (to'lanmagan bron #{pending.id} bor)" if pending else ""
    if weak:
        if db.scalar(select(Alert.id).where(Alert.dedupe_key == f"R2:{room.id}:{hour}")):
            ev.verdict = "R2"
            return
        ev.verdict = "R2-weak"
        alerts.raise_alert(
            db, "R2",
            f"{room.number}-xona eshigida bitta qisqa harakat (tasdiqlanmagan, yonidan o'tgan bo'lishi mumkin){extra}.",
            severity="medium", room=room, booking=pending, zone_event=ev,
            dedupe_key=f"R2w:{room.id}:{hour}", at=ev.occurred_at, data={"strength": "weak"},
        )
        return
    ev.verdict = "R2"
    _mark_corroborated(db, ev)
    alerts.raise_alert(
        db, "R2",
        f"Bronsiz/bo'sh {room.number}-xona eshigida harakat, farrosh belgisi yo'q{extra}.",
        room=room, booking=pending, zone_event=ev,
        dedupe_key=f"R2:{room.id}:{hour}", at=ev.occurred_at, data={"strength": "strong"},
    )


def _raise_r3(db: Session, ev: ZoneEvent, room: Room, w: EntryWindow, weak: bool) -> None:
    ev.verdict = "R3-weak" if weak else "R3"
    if not weak:
        _mark_corroborated(db, ev)
    alerts.raise_alert(
        db, "R3",
        f"Boshqa xona berilgan bo'lishi mumkin: bron #{w.booking_id} {w.room.number}-xona uchun to'langan, "
        f"lekin kirish oynasida bo'sh {room.number}-xona eshigida harakat qayd etildi"
        + (" (bitta qisqa hodisa — videoni tekshiring)." if weak else "."),
        severity="high" if weak else "critical",
        room=room, booking=w.booking, zone_event=ev,
        dedupe_key=f"R3:{w.id}:{room.id}",
        data={"paid_room": w.room.number, "entered_room": room.number, "window_id": w.id, "strength": ev.strength},
        at=ev.occurred_at,
    )


def resolve_suspects(db: Session) -> int:
    """Tasdiqlanmay qolgan kuchsiz hodisalarni past darajada qayd etish."""
    cutoff = clock.now() - timedelta(minutes=cfg.get_int(db, "corroboration_minutes"))
    rows = db.scalars(
        select(ZoneEvent).where(ZoneEvent.verdict.in_(["R2?", "R3?"]), ZoneEvent.occurred_at <= cutoff)
    ).all()
    for ev in rows:
        room = db.get(Room, ev.room_id)
        if ev.verdict == "R3?":
            w = db.scalar(select(EntryWindow).where(EntryWindow.other_room_event_id == ev.id))
            if w is not None:
                _raise_r3(db, ev, room, w, weak=True)
                continue
        _raise_r2(db, ev, room, weak=True)
    return len(rows)


# ------------------------------------------------------------------ camera health
def _open_gap(db: Session, channel: int | None, source: str, start: datetime) -> CoverageGap:
    gap = db.scalar(
        select(CoverageGap).where(
            CoverageGap.channel.is_(None) if channel is None else CoverageGap.channel == channel,
            CoverageGap.source == source,
            CoverageGap.end_at.is_(None),
        )
    )
    if gap is None:
        gap = CoverageGap(channel=channel, source=source, start_at=start)
        db.add(gap)
        db.flush()
    return gap


def _close_gap(db: Session, channel: int | None, source: str, end: datetime) -> CoverageGap | None:
    gap = db.scalar(
        select(CoverageGap).where(
            CoverageGap.channel.is_(None) if channel is None else CoverageGap.channel == channel,
            CoverageGap.source == source,
            CoverageGap.end_at.is_(None),
        )
    )
    if gap is not None:
        gap.end_at = max(end, gap.start_at)
    return gap


def _cam_label(camera: Camera | None, channel: int | None) -> str:
    return f"{camera.name} (kanal {camera.channel})" if camera else f"kanal {channel}"


def process_health_event(db: Session, ev: ZoneEvent, camera: Camera | None) -> None:
    """Kamera yopildi (shelteralarm) yoki signal yo'qoldi (videoloss)."""
    etype = ev.event_type.lower()
    source = "videoloss" if etype == "videoloss" else "tamper"
    reason = (ev.raw or {}).get("reason")
    label = _cam_label(camera, ev.channel)
    if ev.state == "active":
        _open_gap(db, ev.channel, source, ev.occurred_at)
        if camera is not None:
            camera.tampered = True
        ev.verdict = f"health:{source}"
        what = {"covered": "kamera yopildi (tasvir qorong'i)", "moved": "kamera burildi (sahna o'zgardi)"}.get(
            reason, "videosignal yo'qoldi" if source == "videoloss" else "kamera yopildi yoki burildi"
        )
        alerts.raise_alert(
            db, "R12",
            f"{label}: {what}! Shu koridordagi eshiklar kuzatilmayapti. Kimdir kamerani to'sgan bo'lishi mumkin.",
            severity="high", zone_event=ev,
            dedupe_key=f"R12:{source}:{ev.channel}:{clock.local(ev.occurred_at):%Y%m%d%H}",
            data={"kind": source, "channel": ev.channel}, at=ev.occurred_at,
        )
    else:
        gap = _close_gap(db, ev.channel, source, ev.occurred_at)
        if camera is not None:
            camera.tampered = False
        ev.verdict = f"health:{source}:restored"
        if gap is not None:
            mins = int((gap.end_at - gap.start_at).total_seconds() // 60)
            _tell_owners(db, f"✅ {label}: tasvir tiklandi ({mins} daqiqa kuzatuvsiz).")


def heartbeat(db: Session, payload: dict | None = None) -> None:
    payload = payload or {}
    now = clock.now()
    state = dict(cfg.state_get(db, EDGE_HEARTBEAT_KEY) or {})
    state.update({k: v for k, v in payload.items() if k not in ("channels", "gaps")})
    state["at"] = now.isoformat()
    cfg.state_set(db, EDGE_HEARTBEAT_KEY, state)
    if int(payload.get("queue", 0) or 0) == 0:
        cfg.state_set(db, EDGE_SYNCED_KEY, now.isoformat())
    if cfg.state_get(db, EDGE_OFFLINE_SINCE_KEY):
        cfg.state_set(db, EDGE_OFFLINE_SINCE_KEY, None)
        audit.log(db, "edge", "edge.online", object_type="edge", object_id="nvr")
    # NVR oqimi uzilishlari: edge aniq ro'yxat yuboradi, bo'lmasa nvr_ok bo'yicha
    gaps = payload.get("gaps")
    if gaps is not None:
        for g in gaps:
            start, end = _parse_dt(g.get("start")), _parse_dt(g.get("end"))
            if start is None:
                continue
            ch = g.get("channel")
            ch = int(ch) if ch is not None else None
            row = db.scalar(
                select(CoverageGap).where(
                    CoverageGap.channel.is_(None) if ch is None else CoverageGap.channel == ch,
                    CoverageGap.source == "stream",
                    CoverageGap.start_at == start,
                )
            )
            if row is None:
                db.add(CoverageGap(channel=ch, source="stream", start_at=start, end_at=end))
            elif row.end_at is None and end is not None:
                row.end_at = end
        db.flush()
    elif "nvr_ok" in payload:
        if payload["nvr_ok"] is False:
            _open_gap(db, None, "stream", now)
        else:
            _close_gap(db, None, "stream", now)
    # Har bir kanal holati
    for ch_s, online in (payload.get("channels") or {}).items():
        try:
            ch = int(ch_s)
        except (TypeError, ValueError):
            continue
        camera = db.scalar(select(Camera).where(Camera.channel == ch))
        if camera is None:
            continue
        was = camera.online
        camera.online = bool(online)
        camera.status_at = now
        if was is not False and not online:
            _open_gap(db, ch, "channel_offline", now)
            alerts.raise_alert(
                db, "R12",
                f"{_cam_label(camera, ch)} oflayn — shu koridor kuzatilmayapti.",
                severity="high",
                dedupe_key=f"R12:offline:{ch}:{clock.local(now):%Y%m%d%H}",
                data={"kind": "channel_offline", "channel": ch},
            )
        elif was is False and online:
            gap = _close_gap(db, ch, "channel_offline", now)
            mins = int((gap.end_at - gap.start_at).total_seconds() // 60) if gap else 0
            _tell_owners(db, f"✅ {_cam_label(camera, ch)} yana onlayn ({mins} daqiqa uzilish).")


def last_heartbeat(db: Session) -> datetime | None:
    v = cfg.state_get(db, EDGE_HEARTBEAT_KEY)
    return _parse_dt(v["at"]) if v else None


def edge_status(db: Session) -> dict:
    v = cfg.state_get(db, EDGE_HEARTBEAT_KEY) or {}
    hb = last_heartbeat(db)
    limit = cfg.get_int(db, "nvr_offline_minutes")
    online = hb is not None and clock.now() - hb <= timedelta(minutes=limit)
    cams = db.scalars(select(Camera)).all()
    return {
        "last": hb,
        "online": online,
        "nvr_ok": v.get("nvr_ok", online),
        "queue": v.get("queue", 0),
        "synced": _parse_dt(cfg.state_get(db, EDGE_SYNCED_KEY)),
        "cams_offline": [c for c in cams if c.online is False or c.tampered],
        "calibration": calibration_active(db),
    }


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
            db, "R12",
            f"Kamera tizimi: {what} (oxirgi aloqa: {clock.fmt(hb)}). 2-bosqich vaqtincha tekshirilmaydi.",
            dedupe_key=f"R12:edge:{since or clock.now().isoformat()}",
            data={"kind": "nvr"},
        )
        _tell_owners(db, f"🟡 R12: {what}. Oxirgi aloqa: {clock.fmt(hb)}")


def check_camera_silence(db: Session) -> None:
    """Eshik zonalari bor kamera uzoq vaqt hech qanday hodisa bermasa — zona o'chirilgan bo'lishi mumkin."""
    hours = cfg.get_int(db, "camera_silence_hours")
    if not edge_status(db)["online"]:
        return
    cutoff = clock.now() - timedelta(hours=hours)
    door_cams = db.scalars(
        select(Camera).join(CameraZone, CameraZone.camera_id == Camera.id).where(CameraZone.kind == ZoneKind.DOOR).distinct()
    ).all()
    for cam in door_cams:
        if cam.online is False:
            continue
        ref = cam.last_event_at or cam.status_at
        if ref is None or ref < cutoff:
            alerts.raise_alert(
                db, "R12",
                f"{_cam_label(cam, cam.channel)} {hours} soatdan beri birorta hodisa bermadi. "
                f"NVR'da hudud aniqlash o'chirilgan bo'lishi mumkin.",
                dedupe_key=f"R12:silent:{cam.channel}:{clock.today().isoformat()}",
                data={"kind": "silent", "channel": cam.channel},
            )


# ------------------------------------------------------------------ coverage & sweep
def coverage_state(db: Session, room_id: int, start: datetime, end: datetime) -> str:
    """ok / no_zone / gap_closed / gap_open — oyna davomida xona eshigi kuzatilganmi."""
    channels = room_channels(db, room_id)
    if not channels:
        return "no_zone"
    gaps = db.scalars(
        select(CoverageGap).where(
            or_(CoverageGap.channel.is_(None), CoverageGap.channel.in_(channels)),
            CoverageGap.start_at < end,
            or_(CoverageGap.end_at.is_(None), CoverageGap.end_at > start),
        )
    ).all()
    if any(g.end_at is None for g in gaps):
        return "gap_open"
    if gaps:
        return "gap_closed"
    return "ok"


def sweep_windows(db: Session) -> None:
    """Muddati o'tgan kirish oynalarini yopish: R1 yoki «tekshirib bo'lmadi»."""
    now = clock.now()
    hb = last_heartbeat(db)
    limit = timedelta(minutes=cfg.get_int(db, "nvr_offline_minutes"))
    edge_online = hb is not None and now - hb <= limit
    unverifiable_after = timedelta(hours=cfg.get_int(db, "edge_unverifiable_hours"))
    synced = _parse_dt(cfg.state_get(db, EDGE_SYNCED_KEY))
    for w in db.scalars(
        select(EntryWindow).where(EntryWindow.result == WindowResult.OPEN, EntryWindow.closes_at < now)
    ).all():
        booking = w.booking
        if not booking.is_open:
            w.result = WindowResult.SUPERSEDED
            w.closed_at = now
            continue
        cov = coverage_state(db, w.room_id, w.opened_at, w.closes_at)
        if cov == "no_zone":
            mark_unverifiable(db, w, "xona eshigi kamera zonasiga bog'lanmagan")
        elif cov == "gap_closed":
            mark_unverifiable(db, w, "oyna davomida kamera kuzatuvi uzilgan")
        elif cov == "gap_open":
            if edge_online or now - w.closes_at > unverifiable_after:
                mark_unverifiable(db, w, "kamera yoki NVR oqimi ishlamayapti")
        elif synced is not None and synced >= w.closes_at + timedelta(seconds=5):
            # Edge oyna yopilgandan keyin navbatini to'liq yubordi — hodisa haqiqatan yo'q
            w.result = WindowResult.MISSED
            w.closed_at = now
            audit.log(db, "system", "window.missed", w)
            if w.other_room_event_id is None:
                alerts.raise_alert(
                    db, "R1",
                    f"Bron #{booking.id}: to'lov bor, lekin kirish oynasida {w.room.number}-xona eshigida harakat yo'q.",
                    booking=booking, room=w.room, dedupe_key=f"R1:{w.id}",
                )
        elif not edge_online and now - w.closes_at > unverifiable_after:
            mark_unverifiable(db, w, "edge uzoq vaqt aloqasiz")
        # aks holda: edge navbatidagi hodisalarni kutamiz


def mark_unverifiable(db: Session, w: EntryWindow, reason: str = "nvr_offline") -> None:
    w.result = WindowResult.UNVERIFIABLE
    w.closed_at = clock.now()
    bookings.add_confirmation(db, w.booking, 2, ConfirmSource.CAMERA_UNVERIFIABLE, {"window_id": w.id, "reason": reason})
    audit.log(db, "system", "window.unverifiable", w, new={"reason": reason})
    _tell_owners(
        db,
        f"🟡 Bron #{w.booking_id} ({w.room.number}): 2-bosqich «tekshirib bo'lmadi» — {reason}. "
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


def camera_health_rows(db: Session) -> list[dict]:
    """Panel uchun: har bir kamera holati, 24 soatlik hodisalar, noto'g'ri signallar."""
    now = clock.now()
    day = now - timedelta(hours=24)
    month = now - timedelta(days=30)
    rows = []
    for cam in db.scalars(select(Camera).order_by(Camera.channel)):
        zones = db.scalars(select(CameraZone).where(CameraZone.camera_id == cam.id)).all()
        room_ids = [z.room_id for z in zones if z.room_id]
        events_24h = db.scalar(
            select(func.count(ZoneEvent.id)).where(ZoneEvent.camera_id == cam.id, ZoneEvent.occurred_at >= day)
        )
        fp = 0
        if room_ids:
            fp = db.scalar(
                select(func.count(Alert.id)).where(
                    Alert.room_id.in_(room_ids), Alert.status == "false_positive", Alert.at >= month
                )
            )
        gap_minutes = 0
        for g in db.scalars(
            select(CoverageGap).where(
                or_(CoverageGap.channel == cam.channel, CoverageGap.channel.is_(None)),
                or_(CoverageGap.end_at.is_(None), CoverageGap.end_at >= day),
            )
        ):
            s, e = max(g.start_at, day), g.end_at or now
            gap_minutes += max(0, int((e - s).total_seconds() // 60))
        rows.append(
            {
                "cam": cam,
                "doors": len(room_ids),
                "entrance": any(z.kind == ZoneKind.ENTRANCE for z in zones),
                "events_24h": events_24h,
                "false_positives_30d": fp,
                "gap_minutes_24h": gap_minutes,
                "coverage_pct": round(100 - 100 * min(gap_minutes, 1440) / 1440, 1),
            }
        )
    return rows


def unmapped_regions(db: Session, hours: int = 24) -> list[dict]:
    """Sozlash rejimi uchun: oxirgi soatlarda kelgan, xonaga bog'lanmagan (kanal, hudud) juftliklari."""
    since = clock.now() - timedelta(hours=hours)
    rows = db.execute(
        select(ZoneEvent.channel, ZoneEvent.region_id, func.count(ZoneEvent.id), func.max(ZoneEvent.occurred_at))
        .where(ZoneEvent.kind == "unmapped", ZoneEvent.occurred_at >= since, ZoneEvent.channel.is_not(None))
        .group_by(ZoneEvent.channel, ZoneEvent.region_id)
        .order_by(func.max(ZoneEvent.occurred_at).desc())
    ).all()
    return [{"channel": c, "region_id": r, "count": n, "last": last} for c, r, n, last in rows]
