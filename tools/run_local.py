"""Lokal kompyuterda bitta buyruq bilan ishga tushirish (Windows / macOS / Linux).

    python -m tools.run_local            # demo ma'lumotlar + virtual kameralar
    python -m tools.run_local --empty    # bo'sh baza (faqat 40 xona va tariflar)
    python -m tools.run_local --reset    # bazani o'chirib, qaytadan yaratish

Ishga tushadi:
  * panel ............ http://localhost:8000/panel/   (login: owner, parol: ramada123)
  * virtual kameralar  http://localhost:8601/         (tugmalar bilan boshqarish)
  * worker (qoidalar, hisobotlar) va edge agent (kamera tahlili)

Hammasi `local/` papkasida saqlanadi (SQLite). To'xtatish: Ctrl+C.
Telegram bot va SMS lokalda ishlamaydi (token kerak) — SMS'lar konsolga yoziladi.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / "local"
OWNER_LOGIN, OWNER_PASSWORD = "owner", "ramada123"
EDGE_SECRET = "local-edge-secret"


def env(port: int) -> dict:
    e = dict(os.environ)
    e.update(
        SQ_DATABASE_URL=f"sqlite:///{(LOCAL / 'shaffof.db').as_posix()}",
        SQ_STORAGE_DIR=str(LOCAL / "storage"),
        SQ_BASE_URL=f"http://localhost:{port}",
        SQ_SECRET_KEY="local-secret-key-0123456789abcdefghijkl",
        SQ_EDGE_SECRET=EDGE_SECRET,
        SQ_SMS_PROVIDER="console",
        PYTHONPATH=str(ROOT),
        PYTHONUNBUFFERED="1",
    )
    return e


def check_deps() -> None:
    missing = []
    for mod in ("fastapi", "uvicorn", "sqlalchemy", "aiogram", "httpx", "numpy", "cv2", "qrcode", "openpyxl"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    if missing:
        print("❌ Kutubxonalar o'rnatilmagan:", ", ".join(missing))
        print('   O\'rnating:  python -m pip install -e ".[edge]"')
        sys.exit(1)


def prepare(empty: bool, port: int) -> dict:
    os.environ.update({k: v for k, v in env(port).items() if k.startswith("SQ_")})
    from sqlalchemy import select

    from app.cli import cmd_seed_demo, create_tables
    from app.db import session_scope
    from app.enums import Role
    from app.models import Booking, Camera, CameraZone, Room, User
    from app.security import hash_password, make_room_token
    from app.services import bookings, shifts
    from app.services import settings_svc as cfg
    from tools.camsim import DOORS

    LOCAL.mkdir(exist_ok=True)
    fresh = not (LOCAL / "shaffof.db").exists()
    create_tables()
    if fresh:
        cmd_seed_demo(None)
        with session_scope() as db:
            db.add(User(full_name="Ega", role=Role.OWNER, login=OWNER_LOGIN, password_hash=hash_password(OWNER_PASSWORD)))
            cfg.set_value(db, "wifi_ssid", "Ramada-Guest")
            cfg.set_value(db, "wifi_password", "Ramada-2026")
            # Virtual kameralar: 1-kamera → 101..106, 2-kamera → 201..206 (eshik zonalari chizilgan)
            rooms = {r.number: r for r in db.scalars(select(Room))}
            for ch in (1, 2):
                cam = db.scalar(select(Camera).where(Camera.channel == ch))
                for d in DOORS:
                    z = db.scalar(select(CameraZone).where(CameraZone.camera_id == cam.id, CameraZone.region_id == d["door"]))
                    z.room_id, z.polygon = rooms[f"{ch}0{d['door']}"].id, d["zone"]
        if not empty:
            from app.demo import generate

            print("Demo ma'lumotlar yaratilmoqda (14 kun)...")
            with session_scope() as db:
                st = generate(db, days=14)
            print(f"  {st['bookings']} bron, {st['violations']} ta buzilish holati")
    # Har ishga tushganda: ega smenasi ochiq bo'lsin va sinov uchun yangi mehmon broni
    from app.integrations import sms as sms_mod
    from app.integrations import telegram as tg

    tg.set_backend(tg.FakeTelegram())
    sms_mod.set_provider(sms_mod.ConsoleSms())
    from datetime import timedelta

    from app import clock

    with session_scope() as db:
        owner = db.scalar(select(User).where(User.login == OWNER_LOGIN))
        if shifts.current_shift(db, owner) is None:
            shifts.open_shift(db, owner, 0)
        today = clock.today()
        free = [r for r in bookings.available_rooms(db, today, today + timedelta(days=2), require_clean=False)
                if r.number.startswith(("1", "2"))]
        info = {}
        if free:
            room = free[0]
            room.state = "clean"
            b = bookings.create_booking(db, user=owner, room_id=room.id, nights=2, guest_name="Sinov Mehmon",
                                        phone="+998901234567", photo_tg="local")
            info = {"room": room.number, "guest": f"/g/{b.guest_token}", "qr": f"/q/{make_room_token(room.id, room.qr_version)}",
                    "booking": b.id}
        _ = Booking
    return info


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--empty", action="store_true", help="demo ma'lumotlarsiz")
    ap.add_argument("--reset", action="store_true", help="local/ papkasini o'chirib, qaytadan boshlash")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--cam-port", type=int, default=8601)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-cameras", action="store_true")
    a = ap.parse_args()
    check_deps()
    if a.reset and LOCAL.exists():
        shutil.rmtree(LOCAL)
    info = prepare(a.empty, a.port)
    e = env(a.port)
    procs: list[subprocess.Popen] = []
    logs = LOCAL / "logs"
    logs.mkdir(exist_ok=True)

    def run(name: str, args: list[str]):
        p = subprocess.Popen([sys.executable, *args], cwd=ROOT, env=e,
                             stdout=open(logs / f"{name}.log", "w"), stderr=subprocess.STDOUT)
        procs.append(p)

    run("web", ["-m", "uvicorn", "app.main:app", "--host", a.host, "--port", str(a.port)])
    run("worker", ["-m", "app.worker"])
    if not a.no_cameras:
        run("camsim", ["-m", "tools.camsim", "--port", str(a.cam_port), "--cameras", "2", "--host", a.host])
        cfgfile = LOCAL / "edge.toml"
        cfgfile.write_text(
            f'server_url = "http://127.0.0.1:{a.port}"\nedge_secret = "{EDGE_SECRET}"\n'
            f'queue_path = "{(LOCAL / "edge.queue.db").as_posix()}"\nheartbeat_seconds = 20\nconfig_refresh_seconds = 30\n'
            + "".join(
                f'\n[[sources]]\ntype = "rtsp"\nchannel = {i}\nurl = "http://127.0.0.1:{a.cam_port}/cam{i}.mjpg"\n'
                for i in (1, 2)
            )
        )
        time.sleep(1.5)
        run("edge", ["-m", "edge.agent", "--config", str(cfgfile)])

    import httpx

    for _ in range(60):
        try:
            if httpx.get(f"http://127.0.0.1:{a.port}/api/ping", timeout=1).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.5)
    base = f"http://localhost:{a.port}"
    print("\n" + "=" * 64)
    print("✅ Shaffof qabul (Ramada) lokal ishga tushdi")
    print(f"   Panel ............ {base}/panel/   login: {OWNER_LOGIN}  parol: {OWNER_PASSWORD}")
    if not a.no_cameras:
        print(f"   Virtual kameralar  http://localhost:{a.cam_port}/")
    if info:
        print(f"   Sinov mehmoni ({info['room']}-xona):")
        print(f"     1) Mehmon sahifasi: {base}{info['guest']}  → «Tasdiqlayman»")
        print(f"     2) Panel → Bronlar → #{info['booking']} → «To'lov kiritish» (naqd, chek raqami)")
        print(f"     3) Virtual kameralar → «{info['room']}-xona oldida turish» → bron 2/3")
        print(f"     4) Xona QR'i: {base}{info['qr']}  → «Ha» → 3/3 va Wi-Fi paroli")
    print(f"   Loglar: {logs}")
    print("   To'xtatish: Ctrl+C")
    print("=" * 64, flush=True)
    if not a.no_browser:
        try:
            webbrowser.open(f"{base}/panel/")
        except Exception:
            pass

    def shutdown(*_):
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
        print("\nTo'xtatildi.")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)
    while True:
        for p in procs:
            if p.poll() is not None:
                print(f"⚠️ Jarayon to'xtadi (kod {p.returncode}). Loglarni ko'ring: {logs}")
                shutdown()
        time.sleep(2)


if __name__ == "__main__":
    main()
