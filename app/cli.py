"""Boshqaruv buyruqlari.

    python -m app.cli init --owner "Ism" --login owner --password '...'
    python -m app.cli seed-demo            # 40 xona, toifalar, tariflar, kameralar (namuna)
    python -m app.cli report daily         # hisobotni konsolga chiqarish
"""
from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

from app.db import Base, get_engine, session_scope
from app.enums import DayType, Role, ZoneKind
from app.models import Camera, CameraZone, Room, RoomCategory, Tariff, User
from app.security import hash_password, new_invite_code


def create_tables() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(get_engine())


def cmd_init(args) -> None:
    create_tables()
    with session_scope() as db:
        if db.scalar(select(User).where(User.role == Role.OWNER)):
            print("Ega allaqachon mavjud.")
            return
        password = args.password or getpass.getpass("Ega paroli (8+ belgi): ")
        if len(password) < 8:
            sys.exit("Parol kamida 8 belgi bo'lishi kerak")
        u = User(
            full_name=args.owner,
            role=Role.OWNER,
            login=args.login,
            password_hash=hash_password(password),
            invite_code=new_invite_code(),
            lang=args.lang,
        )
        db.add(u)
        db.flush()
        print(f"Ega yaratildi. Panel login: {u.login}. Telegram botda: /start {u.invite_code}")


def cmd_seed_demo(args) -> None:
    """Pilot uchun boshlang'ich tuzilma: 3 qavat × ~13 xona = 40 xona, 3 toifa.

    Haqiqiy narxlar va zona raqamlari auditdan keyin panelda o'zgartiriladi.
    """
    create_tables()
    with session_scope() as db:
        if db.scalar(select(Room.id)):
            print("Xonalar allaqachon mavjud — o'tkazib yuborildi.")
            return
        cats = {
            "STD": RoomCategory(code="STD", name="Standart", capacity=2),
            "DLX": RoomCategory(code="DLX", name="Delyuks", capacity=2),
            "LUX": RoomCategory(code="LUX", name="Lyuks", capacity=3),
        }
        db.add_all(cats.values())
        db.flush()
        prices = {"STD": (500_000, 550_000, 600_000), "DLX": (650_000, 700_000, 750_000), "LUX": (800_000, 880_000, 950_000)}
        for code, (wd, we, hol) in prices.items():
            for dt, p in ((DayType.WEEKDAY, wd), (DayType.WEEKEND, we), (DayType.HOLIDAY, hol)):
                db.add(Tariff(category_id=cats[code].id, day_type=dt, season_id=None, price=p))
        layout = {1: 13, 2: 14, 3: 13}
        for floor, n in layout.items():
            cam = Camera(name=f"{floor}-qavat koridor", channel=floor, floor=floor)
            db.add(cam)
            db.flush()
            for i in range(1, n + 1):
                number = f"{floor}{i:02d}"
                code = "LUX" if i >= n - 1 else ("DLX" if i >= n - 4 else "STD")
                room = Room(number=number, floor=floor, category_id=cats[code].id)
                db.add(room)
                db.flush()
                db.add(CameraZone(camera_id=cam.id, region_id=i, kind=ZoneKind.DOOR, room_id=room.id))
        lobby = Camera(name="Asosiy kirish", channel=10, floor=1)
        db.add(lobby)
        db.flush()
        db.add(CameraZone(camera_id=lobby.id, region_id=1, kind=ZoneKind.ENTRANCE, room_id=None))
        print("Namuna tuzilma yaratildi: 40 xona, 3 toifa, 4 kamera.")


def cmd_report(args) -> None:
    from app.services import reports

    with session_scope() as db:
        fn = {"daily": reports.daily_text, "weekly": reports.weekly_text, "monthly": reports.monthly_text}[args.kind]
        print(fn(db))


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="app.cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("init", help="Jadvallar va ega hisobini yaratish")
    pi.add_argument("--owner", required=True)
    pi.add_argument("--login", default="owner")
    pi.add_argument("--password")
    pi.add_argument("--lang", default="uz")
    pi.set_defaults(fn=cmd_init)
    ps = sub.add_parser("seed-demo", help="40 xonalik namuna tuzilma")
    ps.set_defaults(fn=cmd_seed_demo)
    pr = sub.add_parser("report")
    pr.add_argument("kind", choices=["daily", "weekly", "monthly"])
    pr.set_defaults(fn=cmd_report)
    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
