from enum import StrEnum


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    RECEPTION = "reception"
    HOUSEKEEPER = "housekeeper"


class RoomState(StrEnum):
    """Xonaning texnik/tozalik holati (bandlik bronlardan hisoblanadi)."""

    CLEAN = "clean"
    DIRTY = "dirty"
    CLEANING = "cleaning"
    OUT_OF_ORDER = "out_of_order"


class BookingStatus(StrEnum):
    PENDING = "pending"  # yaratildi, mehmon tasdig'i/to'lov kutilmoqda
    ACTIVE = "active"  # 1/3 dan o'tdi, mehmon yashamoqda
    CHECKED_OUT = "checked_out"
    CANCELLED = "cancelled"


class DiscountStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ConfirmSource(StrEnum):
    PAYMENT = "payment"  # 1-bosqich: to'lov + mehmon tasdig'i
    PAY_LATER = "pay_later"  # 1-bosqich: ega tasdiqlagan «keyin to'lash»
    CAMERA = "camera"  # 2-bosqich
    CAMERA_UNVERIFIABLE = "camera_unverifiable"  # 2-bosqich: NVR ishlamadi
    QR = "qr"  # 3-bosqich
    HOUSEKEEPER = "housekeeper"  # 3-bosqich (smartfonsiz mehmon)


class PaymentMethod(StrEnum):
    PAYME = "payme"
    CLICK = "click"
    CASH = "cash"
    TERMINAL = "terminal"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    CANCELLED = "cancelled"


class WindowResult(StrEnum):
    OPEN = "open"
    ENTERED = "entered"
    MISSED = "missed"
    UNVERIFIABLE = "unverifiable"
    SUPERSEDED = "superseded"  # xona almashdi yoki bron bekor qilindi


class ZoneKind(StrEnum):
    DOOR = "door"  # xona eshigi oldidagi «hududga kirish» zonasi
    ENTRANCE = "entrance"  # asosiy kirishdagi «chiziq kesish»


class QrAnswer(StrEnum):
    YES = "yes"
    OTHER_AMOUNT = "other_amount"
    OTHER_ROOM = "other_room"


class Severity(StrEnum):
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AlertStatus(StrEnum):
    NEW = "new"
    ACK = "ack"
    RESOLVED = "resolved"
    FALSE_POSITIVE = "false_positive"


class AmendmentKind(StrEnum):
    EXTEND = "extend"
    ROOM_CHANGE = "room_change"


class AmendmentStatus(StrEnum):
    PENDING = "pending"  # mehmon tasdig'i va to'lov kutilmoqda
    APPLIED = "applied"
    CANCELLED = "cancelled"


class CancelRequestStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class DayType(StrEnum):
    WEEKDAY = "weekday"
    WEEKEND = "weekend"
    HOLIDAY = "holiday"


SEVERITY_ORDER = {Severity.MEDIUM: 1, Severity.HIGH: 2, Severity.CRITICAL: 3}
