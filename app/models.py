"""Ma'lumotlar modeli (TT 10-bo'lim + ishlash uchun zarur yordamchi jadvallar)."""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app import clock
from app.db import Base, UTCDateTime
from app.enums import (
    AlertStatus,
    AmendmentStatus,
    BookingStatus,
    CancelRequestStatus,
    DiscountStatus,
    PaymentStatus,
    RoomState,
    WindowResult,
    ZoneKind,
)


def _now():
    return clock.now()


class User(Base):
    """Xodimlar va ega. Telegram orqali ishlaydi, ega/administrator panelga parol bilan kiradi."""

    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20), index=True)
    phone: Mapped[str | None] = mapped_column(String(20))
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    lang: Mapped[str] = mapped_column(String(5), default="uz")
    invite_code: Mapped[str | None] = mapped_column(String(32), unique=True)
    login: Mapped[str | None] = mapped_column(String(50), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(200))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class RoomCategory(Base):
    __tablename__ = "room_categories"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    capacity: Mapped[int] = mapped_column(Integer, default=2)


class Room(Base):
    __tablename__ = "rooms"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(10), unique=True)
    floor: Mapped[int] = mapped_column(Integer)
    category_id: Mapped[int] = mapped_column(ForeignKey("room_categories.id"))
    state: Mapped[str] = mapped_column(String(20), default=RoomState.CLEAN)
    qr_version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    category: Mapped[RoomCategory] = relationship()
    zones: Mapped[list["CameraZone"]] = relationship(back_populates="room")


class Season(Base):
    __tablename__ = "seasons"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)  # shu kun ham kiradi
    priority: Mapped[int] = mapped_column(Integer, default=0)


class Holiday(Base):
    __tablename__ = "holidays"
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, unique=True)
    name: Mapped[str] = mapped_column(String(80))


class Tariff(Base):
    """Tarif jadvali: toifa × kun turi × mavsum (mavsum NULL — asosiy narx)."""

    __tablename__ = "tariffs"
    __table_args__ = (UniqueConstraint("category_id", "day_type", "season_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("room_categories.id"))
    day_type: Mapped[str] = mapped_column(String(10))
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id"))
    price: Mapped[int] = mapped_column(Integer)  # so'm, bir tun


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[object] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)


class Guest(Base):
    __tablename__ = "guests"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(150))
    document: Mapped[str | None] = mapped_column(String(40))
    phone: Mapped[str] = mapped_column(String(20), index=True)
    document_photo: Mapped[str | None] = mapped_column(String(255))  # fayl yo'li
    document_photo_tg: Mapped[str | None] = mapped_column(String(255))  # telegram file_id
    lang: Mapped[str | None] = mapped_column(String(5))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class Shift(Base):
    __tablename__ = "shifts"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    opening_cash: Mapped[int] = mapped_column(Integer, default=0)
    expected_cash: Mapped[int | None] = mapped_column(Integer)
    counted_cash: Mapped[int | None] = mapped_column(Integer)
    diff: Mapped[int | None] = mapped_column(Integer)
    handover_diff: Mapped[int | None] = mapped_column(Integer)  # oldingi smena yopilishi bilan farq

    user: Mapped[User] = relationship()


class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    guest_id: Mapped[int] = mapped_column(ForeignKey("guests.id"))
    checkin_date: Mapped[date] = mapped_column(Date)
    checkout_date: Mapped[date] = mapped_column(Date)
    arrival_at: Mapped[datetime | None] = mapped_column(UTCDateTime)  # mehmon keldi
    official_amount: Mapped[int] = mapped_column(Integer)  # tarif bo'yicha
    discount_amount: Mapped[int] = mapped_column(Integer, default=0)
    extra_amount: Mapped[int] = mapped_column(Integer, default=0)  # kech chiqish, uzaytirish...
    final_amount: Mapped[int] = mapped_column(Integer)  # mehmon to'lashi kerak bo'lgan jami
    price_breakdown: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default=BookingStatus.PENDING, index=True)
    stage: Mapped[int] = mapped_column(Integer, default=0)  # 0..3 ketma-ket o'tilgan bosqichlar
    smartphoneless: Mapped[bool] = mapped_column(Boolean, default=False)
    pay_later_approved: Mapped[bool] = mapped_column(Boolean, default=False)
    discount_status: Mapped[str | None] = mapped_column(String(20))
    guest_accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    guest_accepted_amount: Mapped[int | None] = mapped_column(Integer)
    guest_token: Mapped[str] = mapped_column(String(64), unique=True)
    guest_token_expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    guest_device_hash: Mapped[str | None] = mapped_column(String(64))
    sms_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    key_issued_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    stage1_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    stage2_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    stage3_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    qr_confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    qr_reminder_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    checked_out_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("shifts.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    room: Mapped[Room] = relationship()
    guest: Mapped[Guest] = relationship()
    created_by: Mapped[User] = relationship()
    payments: Mapped[list["Payment"]] = relationship(back_populates="booking", order_by="Payment.id")
    confirmations: Mapped[list["Confirmation"]] = relationship(
        back_populates="booking", order_by="Confirmation.id"
    )

    @property
    def nights(self) -> int:
        return (self.checkout_date - self.checkin_date).days

    @property
    def paid_amount(self) -> int:
        return sum(p.amount for p in self.payments if p.status == PaymentStatus.PAID)

    @property
    def balance_due(self) -> int:
        return self.final_amount - self.paid_amount

    @property
    def stage_label(self) -> str:
        return f"{self.stage}/3"

    @property
    def is_open(self) -> bool:
        return self.status in (BookingStatus.PENDING, BookingStatus.ACTIVE)


class Occupancy(Base):
    """Xonaning qaysi bron bilan qachondan qachongacha band bo'lgani (kamera qoidalari uchun)."""

    __tablename__ = "occupancies"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    start_at: Mapped[datetime] = mapped_column(UTCDateTime)
    end_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Confirmation(Base):
    __tablename__ = "confirmations"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    stage: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(30))
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    void: Mapped[bool] = mapped_column(Boolean, default=False)  # xona almashganda eski tasdiqlar

    booking: Mapped[Booking] = relationship(back_populates="confirmations")


class EntryWindow(Base):
    __tablename__ = "entry_windows"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    opened_at: Mapped[datetime] = mapped_column(UTCDateTime)
    closes_at: Mapped[datetime] = mapped_column(UTCDateTime)
    result: Mapped[str] = mapped_column(String(20), default=WindowResult.OPEN, index=True)
    entered_event_id: Mapped[int | None] = mapped_column(ForeignKey("zone_events.id"))
    other_room_event_id: Mapped[int | None] = mapped_column(ForeignKey("zone_events.id"))
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    booking: Mapped[Booking] = relationship()
    room: Mapped[Room] = relationship()


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    method: Mapped[str] = mapped_column(String(20))
    amount: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default=PaymentStatus.PAID)
    external_id: Mapped[str | None] = mapped_column(String(80), index=True)
    receipt_no: Mapped[str | None] = mapped_column(String(60))
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("shifts.id"))
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    amendment_id: Mapped[int | None] = mapped_column(ForeignKey("amendments.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    booking: Mapped[Booking] = relationship(back_populates="payments")


class Discount(Base):
    __tablename__ = "discounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    approver_role: Mapped[str] = mapped_column(String(20))  # admin yoki owner
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default=DiscountStatus.PENDING, index=True)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    tg_messages: Mapped[list] = mapped_column(JSON, default=list)  # [[chat_id, message_id], ...]

    booking: Mapped[Booking] = relationship()
    requested_by: Mapped[User] = relationship(foreign_keys=[requested_by_id])


class PayLaterRequest(Base):
    __tablename__ = "pay_later_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default=CancelRequestStatus.PENDING)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class CancelRequest(Base):
    __tablename__ = "cancel_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    reason: Mapped[str] = mapped_column(Text)
    requested_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    status: Mapped[str] = mapped_column(String(20), default=CancelRequestStatus.PENDING)
    decided_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class Amendment(Base):
    """Uzaytirish yoki xona almashtirish — yangi mehmon tasdig'i va to'lov bilan (FT-06)."""

    __tablename__ = "amendments"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    new_room_id: Mapped[int | None] = mapped_column(ForeignKey("rooms.id"))
    new_checkout_date: Mapped[date | None] = mapped_column(Date)
    extra_amount: Mapped[int] = mapped_column(Integer, default=0)
    breakdown: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default=AmendmentStatus.PENDING, index=True)
    guest_accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    booking: Mapped[Booking] = relationship()
    new_room: Mapped[Room | None] = relationship()


class Camera(Base):
    __tablename__ = "cameras"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80))
    channel: Mapped[int] = mapped_column(Integer, unique=True)  # NVR kanali
    floor: Mapped[int | None] = mapped_column(Integer)
    online: Mapped[bool | None] = mapped_column(Boolean)  # NVR kanal holati (edge xabar beradi)
    status_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_event_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    tampered: Mapped[bool] = mapped_column(Boolean, default=False)  # yopilgan / signal yo'q


class CameraZone(Base):
    """Kamera tasviridagi hudud. door — xona eshigi, entrance — asosiy kirish chizig'i."""

    __tablename__ = "camera_zones"
    __table_args__ = (UniqueConstraint("camera_id", "region_id", "kind"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id"))
    region_id: Mapped[int] = mapped_column(Integer)  # ISAPI regionID / lineID
    kind: Mapped[str] = mapped_column(String(20), default=ZoneKind.DOOR)
    room_id: Mapped[int | None] = mapped_column(ForeignKey("rooms.id"))

    camera: Mapped[Camera] = relationship()
    room: Mapped[Room | None] = relationship(back_populates="zones")


class ZoneEvent(Base):
    __tablename__ = "zone_events"
    __table_args__ = (Index("ix_zone_events_room_time", "room_id", "occurred_at"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    zone_id: Mapped[int | None] = mapped_column(ForeignKey("camera_zones.id"))
    room_id: Mapped[int | None] = mapped_column(ForeignKey("rooms.id"))
    camera_id: Mapped[int | None] = mapped_column(ForeignKey("cameras.id"))
    kind: Mapped[str] = mapped_column(String(20))
    event_type: Mapped[str] = mapped_column(String(40))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    video_link: Mapped[str | None] = mapped_column(String(500))
    snapshot_path: Mapped[str | None] = mapped_column(String(255))
    edge_event_id: Mapped[str | None] = mapped_column(String(80), unique=True)
    verdict: Mapped[str | None] = mapped_column(String(40))  # entered/occupied/cleaning/R2/R2?/R3...
    raw: Mapped[dict | None] = mapped_column(JSON)
    channel: Mapped[int | None] = mapped_column(Integer)  # xom NVR kanali (zona bog'lanmagan bo'lsa ham)
    region_id: Mapped[int | None] = mapped_column(Integer)
    target: Mapped[str | None] = mapped_column(String(20))  # human / vehicle (AcuSense)
    strength: Mapped[str | None] = mapped_column(String(10))  # strong / weak
    state: Mapped[str | None] = mapped_column(String(10))  # health hodisalari: active / inactive
    skew_s: Mapped[int | None] = mapped_column(Integer)  # NVR soati farqi, soniya


class CoverageGap(Base):
    """Kamera kuzatuvi bo'lmagan davr: NVR oqimi uzilgan, kanal oflayn, kamera yopilgan.

    channel = NULL — butun NVR. Shu davrga to'g'ri kelgan kirish oynasi uchun R1 berilmaydi,
    2-bosqich «tekshirib bo'lmadi» deb belgilanadi.
    """

    __tablename__ = "coverage_gaps"
    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[int | None] = mapped_column(Integer, index=True)
    source: Mapped[str] = mapped_column(String(20))  # stream / channel_offline / videoloss / tamper
    start_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    end_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class QrScan(Base):
    __tablename__ = "qr_scans"
    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    device_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    method: Mapped[str | None] = mapped_column(String(20))  # device / last4 / none
    found_booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id"))
    answer: Mapped[str | None] = mapped_column(String(20))
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    ip: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str | None] = mapped_column(String(40))


class CleaningSession(Base):
    __tablename__ = "cleaning_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class RoomCheck(Base):
    """Farroshning ertalabki tekshiruvi va haftalik QR plita tekshiruvi."""

    __tablename__ = "room_checks"
    __table_args__ = (UniqueConstraint("day", "room_id", "kind"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    room_id: Mapped[int] = mapped_column(ForeignKey("rooms.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(20), default="morning")  # morning / qr_plate
    used: Mapped[bool | None] = mapped_column(Boolean)
    qr_ok: Mapped[bool | None] = mapped_column(Boolean)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(primary_key=True)
    rule: Mapped[str] = mapped_column(String(10), index=True)
    severity: Mapped[str] = mapped_column(String(10), index=True)
    message: Mapped[str] = mapped_column(Text)
    room_id: Mapped[int | None] = mapped_column(ForeignKey("rooms.id"))
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))  # aloqador xodim
    shift_id: Mapped[int | None] = mapped_column(ForeignKey("shifts.id"))
    video_link: Mapped[str | None] = mapped_column(String(500))
    zone_event_id: Mapped[int | None] = mapped_column(ForeignKey("zone_events.id"))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)
    status: Mapped[str] = mapped_column(String(20), default=AlertStatus.NEW, index=True)
    note: Mapped[str | None] = mapped_column(Text)
    resolved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    notified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    room: Mapped[Room | None] = relationship()
    booking: Mapped[Booking | None] = relationship()
    user: Mapped[User | None] = relationship(foreign_keys=[user_id])


class Survey(Base):
    __tablename__ = "surveys"
    id: Mapped[int] = mapped_column(primary_key=True)
    booking_id: Mapped[int] = mapped_column(ForeignKey("bookings.id"), unique=True)
    rating: Mapped[int | None] = mapped_column(Integer)
    price_complaint: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_amount_claimed: Mapped[int | None] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class SmsLog(Base):
    __tablename__ = "sms_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    phone: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(30))
    booking_id: Mapped[int | None] = mapped_column(ForeignKey("bookings.id"))
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    provider_ref: Mapped[str | None] = mapped_column(String(80))
    error: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class SystemState(Base):
    """Kalit-qiymat ko'rinishidagi tizim holati (edge heartbeat va h.k.)."""

    __tablename__ = "system_state"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[object] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)


class KeyBoardCheck(Base):
    """FT-46: kalit taxtasining tasodifiy foto-tekshiruvi."""

    __tablename__ = "key_board_checks"
    id: Mapped[int] = mapped_column(primary_key=True)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    photo_tg: Mapped[str | None] = mapped_column(String(255))
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expected_out: Mapped[list] = mapped_column(JSON, default=list)  # band xonalar


class LinenLog(Base):
    """FT-47: choyshab to'plamlari harakati (kir yuvishga berildi / qaytdi)."""

    __tablename__ = "linen_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    sent: Mapped[int] = mapped_column(Integer, default=0)
    returned: Mapped[int] = mapped_column(Integer, default=0)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


class AuditLog(Base):
    """Faqat qo'shiladigan jurnal (PostgreSQL'da trigger UPDATE/DELETE ni taqiqlaydi)."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(80))  # "user:5", "guest", "system", "camera"...
    action: Mapped[str] = mapped_column(String(60), index=True)
    object_type: Mapped[str | None] = mapped_column(String(40))
    object_id: Mapped[str | None] = mapped_column(String(40))
    old: Mapped[dict | None] = mapped_column(JSON)
    new: Mapped[dict | None] = mapped_column(JSON)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)
