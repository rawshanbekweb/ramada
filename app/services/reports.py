"""Hisobotlar (TT 9-bo'lim, FT-43, FT-44) va xodimlar statistikasi (R11)."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.enums import BookingStatus, ConfirmSource, PaymentMethod, PaymentStatus, Role, Severity
from app.models import Alert, Booking, Confirmation, Discount, Payment, Room, Shift, Survey, User
from app.services import alerts as alerts_svc
from app.services import entry, housekeeping
from app.services.pricing import fmt_sum


def _period(start: date, end: date) -> tuple[datetime, datetime]:
    return clock.local_dt(start), clock.local_dt(end)


def payments_in(db: Session, s: datetime, e: datetime) -> list[Payment]:
    return list(
        db.scalars(
            select(Payment).where(Payment.status == PaymentStatus.PAID, Payment.paid_at >= s, Payment.paid_at < e)
        )
    )


def bookings_created(db: Session, s: datetime, e: datetime) -> list[Booking]:
    return list(db.scalars(select(Booking).where(Booking.created_at >= s, Booking.created_at < e)))


def alerts_in(db: Session, s: datetime, e: datetime) -> list[Alert]:
    return list(db.scalars(select(Alert).where(Alert.at >= s, Alert.at < e).order_by(Alert.at)))


def stage_stats(bks: list[Booking]) -> dict:
    valid = [b for b in bks if b.status != BookingStatus.CANCELLED]
    n = len(valid)
    by_stage = Counter(b.stage for b in valid)
    cam_ok = sum(
        1 for b in valid if any(c.stage == 2 and c.source == ConfirmSource.CAMERA and not c.void for c in b.confirmations)
    )
    stage1 = sum(1 for b in valid if b.stage >= 1)
    return {
        "total": n,
        "s0": by_stage.get(0, 0),
        "s1": by_stage.get(1, 0),
        "s2": by_stage.get(2, 0),
        "s3": by_stage.get(3, 0),
        "full_pct": round(100 * by_stage.get(3, 0) / n, 1) if n else None,
        "camera_pct": round(100 * cam_ok / stage1, 1) if stage1 else None,
        "smartphoneless": sum(1 for b in valid if b.smartphoneless),
        "cancelled": len(bks) - n,
    }


def revenue(pays: list[Payment]) -> dict:
    by = defaultdict(int)
    for p in pays:
        by[p.method] += p.amount
    return {
        "total": sum(by.values()),
        "cash": by[PaymentMethod.CASH],
        "terminal": by[PaymentMethod.TERMINAL],
        "by_method": dict(by),
    }


def adr_for_nights(db: Session, start: date, end: date) -> tuple[int | None, int]:
    """O'rtacha bir tunlik narx va sotilgan xona-tunlar soni."""
    rows = db.scalars(
        select(Booking).where(
            Booking.status.in_([BookingStatus.ACTIVE, BookingStatus.CHECKED_OUT]),
            Booking.checkin_date < end,
            Booking.checkout_date > start,
        )
    ).all()
    total, nights = 0, 0
    for b in rows:
        if b.nights <= 0:
            continue
        per_night = (b.official_amount - b.discount_amount) / b.nights
        overlap = (min(b.checkout_date, end) - max(b.checkin_date, start)).days
        if overlap > 0:
            total += per_night * overlap
            nights += overlap
    return (int(total / nights) if nights else None), nights


def stuck_bookings(db: Session) -> list[Booking]:
    now = clock.now()
    return [
        b
        for b in db.scalars(
            select(Booking).where(Booking.status.in_([BookingStatus.PENDING, BookingStatus.ACTIVE])).order_by(Booking.id)
        )
        if b.stage < 3 and now - b.created_at > timedelta(minutes=30)
    ]


def occupancy_overnight(db: Session, day: date) -> tuple[int, int]:
    rooms = [r for r in db.scalars(select(Room).where(Room.active.is_(True)))]
    occ = sum(1 for r in rooms if housekeeping.occupied_overnight(db, r.id, day) is not None)
    return occ, len(rooms)


# ------------------------------------------------------------------ daily
def daily(db: Session, day: date | None = None) -> dict:
    """`day` — hisobot kuni (odatda kecha). Bandlik — `day` dan keyingi tun ertalabi bo'yicha."""
    day = day or (clock.today() - timedelta(days=1))
    s, e = _period(day, day + timedelta(days=1))
    bks = bookings_created(db, s, e)
    pays = payments_in(db, s, e)
    al = alerts_in(db, s, e)
    occ, rooms = occupancy_overnight(db, day + timedelta(days=1))
    adr, sold = adr_for_nights(db, day, day + timedelta(days=1))
    checkins = len(db.scalars(select(Booking.id).where(Booking.stage1_at >= s, Booking.stage1_at < e)).all())
    closed_shifts = list(db.scalars(select(Shift).where(Shift.closed_at >= s, Shift.closed_at < e)))
    return {
        "day": day,
        "occupancy": occ,
        "rooms": rooms,
        "occupancy_pct": round(100 * occ / rooms, 1) if rooms else 0,
        "revenue": revenue(pays),
        "stages": stage_stats(bks),
        "stuck": stuck_bookings(db),
        "alerts": al,
        "alerts_by_sev": Counter(a.severity for a in al),
        "shifts": closed_shifts,
        "cash_diff": sum(x.diff or 0 for x in closed_shifts),
        "entrances": entry.entrance_count(db, s, e),
        "checkins": checkins,
        "adr": adr,
        "sold_nights": sold,
        "edge": entry.edge_status(db),
    }


def daily_text(db: Session, day: date | None = None) -> str:
    d = daily(db, day)
    st = d["stages"]
    rv = d["revenue"]
    lines = [
        f"📊 <b>Kunlik hisobot — {d['day']:%d.%m.%Y}</b>",
        f"🏨 Bandlik (tun): {d['occupancy']}/{d['rooms']} ({d['occupancy_pct']}%) · ADR: {fmt_sum(d['adr'])}",
        f"💰 Tushum: {fmt_sum(rv['total'])} (naqd {fmt_sum(rv['cash'])}, terminal {fmt_sum(rv['terminal'])})",
        f"✅ Bronlar: {st['total']} · 3/3: {st['s3']} ({st['full_pct'] if st['full_pct'] is not None else '—'}%) · "
        f"2/3: {st['s2']} · 1/3: {st['s1']} · 0/3: {st['s0']} · smartfonsiz: {st['smartphoneless']}",
        f"🎥 Kamera 2-bosqich: {st['camera_pct'] if st['camera_pct'] is not None else '—'}% · "
        f"Kirish sanog'i: {d['entrances']} / joylashtirish: {d['checkins']}",
    ]
    if d["stuck"]:
        lines.append("⏸ To'xtab qolgan bronlar: " + ", ".join(f"#{b.id} {b.room.number} ({b.stage}/3)" for b in d["stuck"][:20]))
    sev = d["alerts_by_sev"]
    lines.append(
        f"🚨 Ogohlantirishlar: kritik {sev.get(Severity.CRITICAL, 0)}, yuqori {sev.get(Severity.HIGH, 0)}, o'rta {sev.get(Severity.MEDIUM, 0)}"
    )
    for a in d["alerts"]:
        if a.severity == Severity.MEDIUM:
            lines.append(f"  🟡 {a.rule}: {a.message[:140]}")
    if d["shifts"]:
        lines.append(
            "💵 Kassa: "
            + "; ".join(f"{x.user.full_name}: farq {fmt_sum(x.diff)}" for x in d["shifts"])
        )
    if not d["edge"]["online"]:
        lines.append(f"⚠️ Kamera tizimi bilan aloqa yo'q (oxirgi: {clock.fmt(d['edge']['last'])})")
    return "\n".join(lines)


# ------------------------------------------------------------------ shift
def shift_report_text(db: Session, shift: Shift) -> str:
    bks = list(db.scalars(select(Booking).where(Booking.shift_id == shift.id)))
    st = stage_stats(bks)
    pays = list(db.scalars(select(Payment).where(Payment.shift_id == shift.id, Payment.status == PaymentStatus.PAID)))
    rv = revenue(pays)
    lines = [
        f"💵 <b>Smena yopildi</b> — {shift.user.full_name}",
        f"🕒 {clock.fmt(shift.opened_at)} → {clock.fmt(shift.closed_at)}",
        f"Joylashtirishlar: {st['total']} · 3/3: {st['s3']} · 2/3: {st['s2']} · 1/3: {st['s1']} · 0/3: {st['s0']}",
        f"To'lovlar: {fmt_sum(rv['total'])} (naqd {fmt_sum(rv['cash'])}, terminal {fmt_sum(rv['terminal'])})",
        f"Kassa: ochilish {fmt_sum(shift.opening_cash)}, kutilgan {fmt_sum(shift.expected_cash)}, sanalgan {fmt_sum(shift.counted_cash)}",
        ("✅ Farq yo'q" if not shift.diff else f"❗ Farq: {fmt_sum(shift.diff)}"),
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ staff (FT-44, R11)
def staff_table(db: Session, start: date, end: date) -> list[dict]:
    s, e = _period(start, end)
    bks = bookings_created(db, s, e)
    by_user: dict[int, list[Booking]] = defaultdict(list)
    for b in bks:
        by_user[b.created_by_id].append(b)
    discounts = db.scalars(select(Discount).where(Discount.requested_at >= s, Discount.requested_at < e)).all()
    shifts = db.scalars(select(Shift).where(Shift.closed_at >= s, Shift.closed_at < e)).all()
    users = {u.id: u for u in db.scalars(select(User).where(User.role.in_([Role.RECEPTION, Role.ADMIN, Role.OWNER])))}
    rows = []
    for uid in set(by_user) | {x.user_id for x in shifts}:
        u = users.get(uid)
        if u is None:
            continue
        st = stage_stats(by_user.get(uid, []))
        my_disc = [d for d in discounts if d.requested_by_id == uid]
        rows.append(
            {
                "user": u,
                "bookings": st["total"],
                "full_pct": st["full_pct"],
                "smartphoneless_pct": round(100 * st["smartphoneless"] / st["total"], 1) if st["total"] else None,
                "discounts": len(my_disc),
                "discount_sum": sum(d.amount for d in my_disc if d.status == "approved"),
                "cash_diff": sum(x.diff or 0 for x in shifts if x.user_id == uid),
                "alerts": len(
                    db.scalars(select(Alert.id).where(Alert.user_id == uid, Alert.at >= s, Alert.at < e)).all()
                ),
            }
        )
    rows.sort(key=lambda r: r["user"].full_name)
    return rows


def staff_outliers(db: Session, rows: list[dict], start: date) -> list[Alert]:
    from app.services import settings_svc as cfg

    pp = cfg.get_int(db, "r11_threshold_pp")
    min_n = cfg.get_int(db, "r11_min_bookings")
    eligible = [r for r in rows if r["bookings"] >= min_n]
    if len(eligible) < 2:
        return []
    out = []
    for metric, label, worse_if_lower in (("full_pct", "3/3 ulushi", True), ("smartphoneless_pct", "smartfonsiz ulushi", False)):
        for r in eligible:
            others = [x[metric] for x in eligible if x is not r and x[metric] is not None]
            if not others or r[metric] is None:
                continue
            avg = sum(others) / len(others)
            delta = r[metric] - avg
            if (worse_if_lower and delta <= -pp) or (not worse_if_lower and delta >= pp):
                a = alerts_svc.raise_alert(
                    db,
                    "R11",
                    f"{r['user'].full_name}: {label} {r[metric]}% (boshqalar o'rtachasi {avg:.1f}%).",
                    user=r["user"],
                    dedupe_key=f"R11:{metric}:{r['user'].id}:{start.isoformat()}",
                )
                if a:
                    out.append(a)
    return out


def weekly(db: Session, week_start: date | None = None) -> dict:
    if week_start is None:
        today = clock.today()
        week_start = today - timedelta(days=today.weekday() + 7)
    end = week_start + timedelta(days=7)
    s, e = _period(week_start, end)
    rows = staff_table(db, week_start, end)
    al = alerts_in(db, s, e)
    rooms_alerts = Counter(a.room.number for a in al if a.room is not None)
    adr, sold = adr_for_nights(db, week_start, end)
    bks = bookings_created(db, s, e)
    return {
        "start": week_start,
        "end": end,
        "staff": rows,
        "adr": adr,
        "sold_nights": sold,
        "stages": stage_stats(bks),
        "repeated": [(room, n) for room, n in rooms_alerts.most_common() if n >= 2],
        "alerts_by_rule": Counter(a.rule for a in al),
        "medium": [a for a in al if a.severity == Severity.MEDIUM],
        "revenue": revenue(payments_in(db, s, e)),
        "linen": housekeeping.linen_balance(db, week_start, end),
    }


def weekly_text(db: Session, week_start: date | None = None, raise_r11: bool = True) -> str:
    w = weekly(db, week_start)
    if raise_r11:
        staff_outliers(db, w["staff"], w["start"])
    st = w["stages"]
    lines = [
        f"📈 <b>Haftalik hisobot — {w['start']:%d.%m} – {(w['end'] - timedelta(days=1)):%d.%m.%Y}</b>",
        f"💰 Tushum: {fmt_sum(w['revenue']['total'])} · ADR: {fmt_sum(w['adr'])} · sotilgan tunlar: {w['sold_nights']}",
        f"✅ 3/3 ulushi: {st['full_pct'] if st['full_pct'] is not None else '—'}% · smartfonsiz: {st['smartphoneless']}",
        "",
        "👥 <b>Xodimlar</b> (bron · 3/3 · smartfonsiz · chegirma · kassa farqi):",
    ]
    for r in w["staff"]:
        lines.append(
            f"• {r['user'].full_name}: {r['bookings']} · {r['full_pct'] if r['full_pct'] is not None else '—'}% · "
            f"{r['smartphoneless_pct'] if r['smartphoneless_pct'] is not None else '—'}% · {r['discounts']} ({fmt_sum(r['discount_sum'])}) · "
            f"{fmt_sum(r['cash_diff'])}"
        )
    if w["repeated"]:
        lines.append("🔁 Takrorlangan ogohlantirishlar: " + ", ".join(f"{room} ×{n}" for room, n in w["repeated"]))
    if w["alerts_by_rule"]:
        lines.append("🚨 Qoidalar: " + ", ".join(f"{k}: {v}" for k, v in sorted(w["alerts_by_rule"].items())))
    ln = w["linen"]
    if ln["sent"] or ln["returned"]:
        lines.append(f"🧺 Choyshab: berildi {ln['sent']}, qaytdi {ln['returned']}, chiqishlar {ln['checkouts']} (farq {ln['diff']})")
    return "\n".join(lines)


# ------------------------------------------------------------------ monthly (KPI, TT 3-bo'lim)
KPI_TARGETS = {
    "full_pct": 85.0,
    "camera_pct": 95.0,
    "r2_count": 2,
    "r5_count": 0,
    "cash_diff": 0,
}


def monthly(db: Session, month_start: date | None = None) -> dict:
    if month_start is None:
        first = clock.today().replace(day=1)
        month_start = (first - timedelta(days=1)).replace(day=1)
    nxt = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    s, e = _period(month_start, nxt)
    bks = bookings_created(db, s, e)
    st = stage_stats(bks)
    al = alerts_in(db, s, e)
    rules = Counter(a.rule for a in al)
    shifts = db.scalars(select(Shift).where(Shift.closed_at >= s, Shift.closed_at < e)).all()
    pays = payments_in(db, s, e)
    by_day: dict[date, int] = defaultdict(int)
    for p in pays:
        by_day[clock.local(p.paid_at).date()] += p.amount
    adr, sold = adr_for_nights(db, month_start, nxt)
    surveys = db.scalars(select(Survey).where(Survey.at >= s, Survey.at < e)).all()
    unapproved_discounts = sum(1 for b in bks if b.discount_amount and b.discount_status != "approved")
    return {
        "start": month_start,
        "end": nxt,
        "stages": st,
        "kpi": {
            "full_pct": st["full_pct"],
            "camera_pct": st["camera_pct"],
            "r2_count": rules.get("R2", 0) + rules.get("R3", 0),
            "unapproved_discounts": unapproved_discounts,
            "r5_count": rules.get("R5", 0),
            "cash_diff": sum(abs(x.diff or 0) for x in shifts),
            "adr": adr,
        },
        "revenue": revenue(pays),
        "by_day": dict(sorted(by_day.items())),
        "sold_nights": sold,
        "complaints": [x for x in surveys if x.price_complaint],
        "ratings": [x.rating for x in surveys if x.rating],
        "alerts_by_rule": rules,
    }


def monthly_text(db: Session, month_start: date | None = None) -> str:
    m = monthly(db, month_start)
    k = m["kpi"]

    def mark(ok):
        return "✅" if ok else "❌"

    lines = [
        f"🗓 <b>Oylik hisobot — {m['start']:%m.%Y}</b>",
        f"{mark((k['full_pct'] or 0) >= 85)} 3/3 ulushi: {k['full_pct'] if k['full_pct'] is not None else '—'}% (maqsad ≥85%)",
        f"{mark((k['camera_pct'] or 0) >= 95)} Kamera 2-bosqich: {k['camera_pct'] if k['camera_pct'] is not None else '—'}% (maqsad ≥95%)",
        f"{mark(k['r2_count'] < 2)} Bronsiz xonada harakat: {k['r2_count']} (maqsad <2)",
        f"{mark(k['unapproved_discounts'] == 0)} Tasdiqsiz chegirma: {k['unapproved_discounts']} (maqsad 0)",
        f"{mark(k['r5_count'] == 0)} «Boshqa summa/xona» javoblari: {k['r5_count']} (maqsad 0)",
        f"{mark(k['cash_diff'] == 0)} Kassa farqi: {fmt_sum(k['cash_diff'])} (maqsad 0)",
        f"💰 Tushum: {fmt_sum(m['revenue']['total'])} · ADR: {fmt_sum(k['adr'])} · sotilgan tunlar: {m['sold_nights']}",
        f"📝 Shikoyatlar: {len(m['complaints'])} · o'rtacha baho: "
        + (f"{sum(m['ratings']) / len(m['ratings']):.1f}" if m["ratings"] else "—"),
    ]
    return "\n".join(lines)


def today_snapshot_text(db: Session) -> str:
    """Botdagi «📊 Bugun» tugmasi uchun joriy holat."""
    open_b = list(
        db.scalars(select(Booking).where(Booking.status.in_([BookingStatus.PENDING, BookingStatus.ACTIVE])))
    )
    active = [b for b in open_b if b.status == BookingStatus.ACTIVE]
    pending = [b for b in open_b if b.status == BookingStatus.PENDING]
    rooms = db.scalars(select(Room).where(Room.active.is_(True))).all()
    s = clock.local_dt(clock.today())
    pays = payments_in(db, s, clock.now() + timedelta(seconds=1))
    rv = revenue(pays)
    open_alerts = db.scalars(select(Alert).where(Alert.status == "new")).all()
    ed = entry.edge_status(db)
    counts = Counter(b.stage for b in active)
    return "\n".join(
        [
            f"📊 <b>Hozir</b> — {clock.fmt(clock.now())}",
            f"🏨 Band: {len(active)}/{len(rooms)} · kutilmoqda: {len(pending)}",
            f"✅ 3/3: {counts.get(3, 0)} · 2/3: {counts.get(2, 0)} · 1/3: {counts.get(1, 0)}",
            f"💰 Bugungi tushum: {fmt_sum(rv['total'])} (naqd {fmt_sum(rv['cash'])})",
            f"🚨 Ochiq ogohlantirishlar: {len(open_alerts)}",
            f"🎥 Kamera: {'✅ aloqa bor' if ed['online'] else '❌ aloqa yo`q'}",
        ]
    )
