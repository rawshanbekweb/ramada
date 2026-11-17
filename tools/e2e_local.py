"""To'liq lokal sinov: server + haqiqiy edge agent + virtual kameralar.

    python -m tools.e2e_local

Nima ishga tushadi (hammasi shu kompyuterda, vaqtinchalik papkada):
  * kamera simulyatori (tools/camsim.py): 2 ta oddiy kamera (MJPEG) va soxta Hikvision (ISAPI);
  * server (uvicorn, SQLite fayl);
  * edge agent: 2 ta `rtsp` manba (video tahlil mini-PC'da) va 1 ta `hikvision` manba.

So'ng mehmonxonadagi haqiqiy vaziyatlar ketma-ket o'ynaladi va natija jadval bilan
chiqariladi. Kod 0 — hammasi o'tdi.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
CAM_PORT, WEB_PORT, RTSP_PORT = 8601, 8610, 8554
# --rtsp: kameralar haqiqiy RTSP/H.264 orqali (tools/rtsp_relay.py, GStreamer kerak)
USE_RTSP = "--rtsp" in sys.argv
TMP = Path(tempfile.mkdtemp(prefix="shaffof-e2e-"))
ENV = dict(
    os.environ,
    SQ_DATABASE_URL=f"sqlite:///{TMP}/e2e.db",
    SQ_BASE_URL=f"http://127.0.0.1:{WEB_PORT}",
    SQ_SECRET_KEY="e2e-secret-key-0123456789abcdefghij",
    SQ_EDGE_SECRET="e2e-edge-secret",
    SQ_STORAGE_DIR=str(TMP / "storage"),
    SQ_SMS_PROVIDER="console",
    PYTHONPATH=str(ROOT),
)
os.environ.update({k: v for k, v in ENV.items() if k.startswith("SQ_")})

PROCS: dict[str, subprocess.Popen] = {}
RESULTS: list[tuple[str, bool, str]] = []


def start(name: str, args: list[str]) -> None:
    log = open(TMP / f"{name}.log", "w")
    PROCS[name] = subprocess.Popen(args, cwd=ROOT, env=ENV, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)


def stop(name: str) -> None:
    p = PROCS.pop(name, None)
    if p and p.poll() is None:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(10)


def wait_until(fn, timeout: float, step: float = 0.5):
    end = time.time() + timeout
    while time.time() < end:
        v = fn()
        if v:
            return v
        time.sleep(step)
    return None


def check(name: str, ok, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(("  ✅ " if ok else "  ❌ ") + name + (f" — {detail}" if detail else ""), flush=True)


def cam(cmd: dict) -> None:
    httpx.post(f"http://127.0.0.1:{CAM_PORT}/control", json=cmd, timeout=5)


# ------------------------------------------------------------------ setup
def setup_db():
    from sqlalchemy import select

    from app.cli import cmd_seed_demo, create_tables
    from app.db import session_scope
    from app.enums import Role, ZoneKind
    from app.models import Camera, CameraZone, Room, User
    from app.security import hash_password
    from tools.camsim import DOORS

    create_tables()
    cmd_seed_demo(None)
    with session_scope() as db:
        db.add_all([
            User(full_name="Ega", role=Role.OWNER, login="owner", password_hash=hash_password("owner-pass-1")),
            User(full_name="Resepshn", role=Role.RECEPTION),
            User(full_name="Farrosh", role=Role.HOUSEKEEPER),
        ])
        rooms = {r.number: r for r in db.scalars(select(Room))}
        # Simulyator: 1-kamera → 101..106, 2-kamera → 201..206 (eshik zonasi — kadr ustidagi ko'pburchak)
        for ch in (1, 2):
            cam_row = db.scalar(select(Camera).where(Camera.channel == ch))
            for z in db.scalars(select(CameraZone).where(CameraZone.camera_id == cam_row.id)):
                db.delete(z)
            db.flush()
            for d in DOORS:
                db.add(CameraZone(camera_id=cam_row.id, region_id=d["door"], kind=ZoneKind.DOOR,
                                  room_id=rooms[f"{ch}0{d['door']}"].id, polygon=d["zone"]))
        # Soxta Hikvision: kanal 1,2 → serverda 21,22 (channel_offset=20); hudud 1..3 → 301..303
        for ch, name in ((21, "Hik sim 1"), (22, "Hik sim 2")):
            c = Camera(name=name, channel=ch, floor=3)
            db.add(c)
            db.flush()
            if ch == 21:
                for i in (1, 2, 3):
                    db.add(CameraZone(camera_id=c.id, region_id=i, kind=ZoneKind.DOOR, room_id=rooms[f"30{i}"].id))


def cam_url(i: int) -> str:
    if USE_RTSP:
        return f"rtsp://admin:Sim12345@127.0.0.1:{RTSP_PORT}/Streaming/Channels/{i}02"
    return f"http://127.0.0.1:{CAM_PORT}/cam{i}.mjpg"


def write_edge_config() -> Path:
    cfg = TMP / "edge.toml"
    cfg.write_text(f'''
server_url = "http://127.0.0.1:{WEB_PORT}"
edge_secret = "e2e-edge-secret"
queue_path = "{TMP}/edge.queue.db"
heartbeat_seconds = 3
config_refresh_seconds = 3

[[sources]]
type = "rtsp"
channel = 1
url = "{cam_url(1)}"
fps = 8

[[sources]]
type = "rtsp"
channel = 2
url = "{cam_url(2)}"
fps = 8

[[sources]]
type = "hikvision"
host = "127.0.0.1:{CAM_PORT}"
user = "admin"
password = "Sim12345"
channels = [1, 2]
channel_offset = 20
''')
    return cfg


# ------------------------------------------------------------------ helpers (DB)
def db_get(fn):
    from app.db import SessionLocal

    with SessionLocal() as s:
        return fn(s)


def alerts(rule: str, room: str | None = None, severity: str | None = None):
    from sqlalchemy import select

    from app.models import Alert, Room

    def q(s):
        stmt = select(Alert).where(Alert.rule == rule)
        if room:
            stmt = stmt.where(Alert.room_id == s.scalar(select(Room.id).where(Room.number == room)))
        if severity:
            stmt = stmt.where(Alert.severity == severity)
        return [(a.id, a.severity, a.message) for a in s.scalars(stmt)]

    return db_get(q)


def make_paid_booking(room: str, phone: str) -> tuple[int, str]:
    """Reception bron yaratadi; mehmon telefonida (HTTP) tasdiqlaydi; reception naqd to'lovni chek bilan kiritadi."""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import Booking, Room, User
    from app.services import bookings, shifts

    with session_scope() as db:
        rec = db.scalar(select(User).where(User.role == "reception"))
        if shifts.current_shift(db, rec) is None:
            shifts.open_shift(db, rec, 0)
        r = db.scalar(select(Room).where(Room.number == room))
        b = bookings.create_booking(db, user=rec, room_id=r.id, nights=1, guest_name="Sinov Mehmon", phone=phone,
                                    photo_tg="sim")
        bid, token = b.id, b.guest_token
    guest = httpx.Client(base_url=f"http://127.0.0.1:{WEB_PORT}", follow_redirects=True, timeout=10)
    page = guest.get(f"/g/{token}")
    assert page.status_code == 200
    guest.post(f"/g/{token}/accept")
    with session_scope() as db:
        rec = db.scalar(select(User).where(User.role == "reception"))
        b = db.get(Booking, bid)
        bookings.record_payment(db, b, "cash", b.final_amount, user=rec, receipt_no=f"SIM-{bid}")
    guest._sq_token = token  # type: ignore[attr-defined]
    PHONES[bid] = guest
    return bid, token


PHONES: dict[int, httpx.Client] = {}


def stage(bid: int) -> int:
    from app.models import Booking

    return db_get(lambda s: s.get(Booking, bid).stage)


# ------------------------------------------------------------------ scenario
def scenario() -> None:
    from sqlalchemy import select

    from app.models import Camera, CoverageGap, ZoneEvent

    print("\n▶ 1. Ulanish va kadrlar")
    ok = wait_until(lambda: db_get(lambda s: all(c.snapshot_path for c in s.scalars(select(Camera).where(Camera.channel.in_([1, 2]))))), 40)
    check("Ikkala oddiy kamera kadri panelga keldi (zonalar chizish uchun)", ok)
    hb = wait_until(lambda: db_get(lambda s: s.scalar(select(Camera.online).where(Camera.channel == 21))), 90)
    check("Hikvision kanallari holati keldi (onlayn)", hb)
    time.sleep(4)  # harakat tahlili fonni o'rgansin

    print("\n▶ 1b. Bo'sh koridor: 60 soniya shovqin va yorug'likning sekin o'zgarishi")
    n0 = db_get(lambda s: len(s.scalars(select(ZoneEvent.id).where(ZoneEvent.kind == "door")).all()))
    time.sleep(60)
    n1 = db_get(lambda s: len(s.scalars(select(ZoneEvent.id).where(ZoneEvent.kind == "door")).all()))
    check("Bo'sh koridorda birorta ham eshik hodisasi yo'q", n1 == n0, f"{n1 - n0} ta hodisa")

    print("\n▶ 1c. Koridorda chiroq o'chdi va yondi (ikkala kamera)")
    for c in (1, 2):
        cam({"cam": c, "action": "lights", "level": 0.45, "seconds": 8})
    time.sleep(14)
    n2 = db_get(lambda s: len(s.scalars(select(ZoneEvent.id).where(ZoneEvent.kind == "door")).all()))
    check("Chiroq o'chib-yonishi yolg'on signal bermadi", n2 == n1, f"{n2 - n1} ta hodisa")

    print("\n▶ 2. Mehmon to'ladi va o'z xonasiga kirdi (102, 1-kamera, 2-eshik)")
    b1, _ = make_paid_booking("102", "+998901000001")
    check("To'lov va mehmon tasdig'idan keyin bron 1/3", stage(b1) == 1)
    t0 = time.time()
    cam({"cam": 1, "action": "stand", "door": 2, "seconds": 5})
    ok = wait_until(lambda: stage(b1) >= 2, 30)
    check("Kamera kirishni tasdiqladi → 2/3", ok, f"{time.time() - t0:.1f} soniyada")

    print("\n▶ 3. Mehmon xonadagi QR'ni skanerladi (telefon brauzeri orqali)")
    from app.models import Room
    from app.security import make_room_token

    rid = db_get(lambda s: s.scalar(select(Room.id).where(Room.number == "102")))
    phone = PHONES[b1]
    page = phone.get(f"/q/{make_room_token(rid, 1)}")
    import re

    m = re.search(r'action="(/q/[^"?]+/answer/\d+)', page.text)
    phone.post(m.group(1), data={"answer": "yes"}) if m else None
    check("QR «Ha» → 3/3 (to'liq tasdiqlangan)", stage(b1) == 3)

    print("\n▶ 4. Koridordan shunchaki o'tib ketgan odam")
    before = len(alerts("R2", severity="high"))
    cam({"cam": 1, "action": "pass", "seconds": 3})
    time.sleep(7)
    check("O'tib ketgan odam yuqori darajali signal bermadi", len(alerts("R2", severity="high")) == before)

    print("\n▶ 5. Bo'sh xona eshigida turish (204, 2-kamera, 4-eshik)")
    t0 = time.time()
    cam({"cam": 2, "action": "stand", "door": 4, "seconds": 5})
    ok = wait_until(lambda: alerts("R2", room="204", severity="high"), 30)
    check("R2 (yuqori) — bronsiz xonaga kirish", ok, f"{time.time() - t0:.1f} soniyada")
    ev = db_get(lambda s: s.scalars(select(ZoneEvent).where(ZoneEvent.verdict == "R2")).first())
    check("Hodisa kadri (snapshot) saqlandi", ev is not None and ev.snapshot_path)

    print("\n▶ 6. 201 uchun to'lab, 205 ga kirdi")
    b2, _ = make_paid_booking("201", "+998901000002")
    t0 = time.time()
    cam({"cam": 2, "action": "stand", "door": 5, "seconds": 5})
    ok = wait_until(lambda: alerts("R3", room="205", severity="critical"), 30)
    check("R3 (kritik) — boshqa xonaga kirildi", ok, f"{time.time() - t0:.1f} soniyada")
    cam({"cam": 2, "action": "stand", "door": 1, "seconds": 5})
    ok = wait_until(lambda: stage(b2) >= 2, 30)
    check("Keyin o'z xonasiga (201) ham kirdi → 2/3, oyna yopildi", ok)

    print("\n▶ 7. Kamera yopildi (1-kamera, 20 soniya)")
    t0 = time.time()
    cam({"cam": 1, "action": "cover", "seconds": 20})
    ok = wait_until(lambda: [a for a in alerts("R12") if "yopildi" in a[2]], 40)
    check("R12 (yuqori) — kamera yopildi", ok, f"{time.time() - t0:.1f} soniyada")
    ok = wait_until(lambda: db_get(lambda s: s.scalar(select(CoverageGap.end_at).where(CoverageGap.channel == 1, CoverageGap.source == "tamper"))), 40)
    check("Ochilgach kuzatuv tiklandi va uzilish davri yozildi", ok)

    print("\n▶ 8. Kamera o'chdi (2-kamera, 15 soniya)")
    cam({"cam": 2, "action": "offline", "seconds": 15})
    ok = wait_until(lambda: [a for a in alerts("R12") if "oflayn" in a[2]], 40)
    check("R12 (yuqori) — kamera oflayn", ok)
    ok = wait_until(lambda: db_get(lambda s: s.scalar(select(CoverageGap.end_at).where(CoverageGap.channel == 2, CoverageGap.source == "stream"))), 90)
    check("Qayta ulandi, uzilish davri faqat 2-kanalga yozildi", ok)

    print("\n▶ 9. Hikvision qurilma hodisalari")
    cam({"hik": True, "channel": 1, "type": "fielddetection", "region": 3})
    ok = wait_until(lambda: alerts("R2", room="303", severity="high"), 20)
    check("Hikvision intrusion → R2 (303)", ok)
    cam({"hik": True, "channel": 2, "type": "shelteralarm", "state": "active"})
    ok = wait_until(lambda: [a for a in alerts("R12") if "Hik sim 2" in a[2]], 20)
    check("Hikvision kamera yopildi → R12", ok)

    print("\n▶ 10. Internet uzildi (server o'chirildi), shu payt bo'sh xonaga kirildi (103)")
    stop("web")
    cam({"cam": 1, "action": "stand", "door": 3, "seconds": 5})
    time.sleep(10)
    start("web", [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(WEB_PORT)])
    ok = wait_until(lambda: alerts("R2", room="103", severity="high"), 60)
    check("Aloqa tiklangach navbatdagi hodisa yetib keldi → R2 (103)", ok)
    if ok:
        ev = db_get(lambda s: s.scalars(select(ZoneEvent).where(ZoneEvent.verdict == "R2", ZoneEvent.room_id == s.scalar(select(Room.id).where(Room.number == "103")))).first())
        lag = (ev.received_at - ev.occurred_at).total_seconds()
        check("Hodisa vaqti — haqiqiy vaqt (kechikib kelgan bo'lsa ham)", lag > 5, f"{lag:.0f} s kechikib keldi, vaqti to'g'ri")


def main() -> int:
    print(f"Vaqtinchalik papka: {TMP}")
    setup_db()
    start("camsim", [sys.executable, "-m", "tools.camsim", "--port", str(CAM_PORT), "--cameras", "2"])
    start("web", [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(WEB_PORT)])
    if USE_RTSP:
        start("relay", ["/usr/bin/python3.12", "tools/rtsp_relay.py", "--port", str(RTSP_PORT), "--cameras", "2"])
        time.sleep(2)
    print("Kameralar:", cam_url(1), cam_url(2))
    if not wait_until(lambda: _up(f"http://127.0.0.1:{WEB_PORT}/api/ping") and _up(f"http://127.0.0.1:{CAM_PORT}/zones.json"), 30):
        print("Server yoki simulyator ishga tushmadi")
        return 2
    start("edge", [sys.executable, "-m", "edge.agent", "--config", str(write_edge_config())])
    try:
        scenario()
    finally:
        for n in list(PROCS):
            stop(n)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\nNatija: {passed}/{len(RESULTS)} o'tdi. Loglar: {TMP}")
    (TMP / "results.json").write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1))
    return 0 if passed == len(RESULTS) else 1


def _up(url: str) -> bool:
    try:
        return httpx.get(url, timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


if __name__ == "__main__":
    sys.exit(main())
