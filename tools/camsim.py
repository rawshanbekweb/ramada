"""Kamera simulyatori — haqiqiy kamerasiz butun zanjirni sinash uchun.

Ikki xil qurilmani taqlid qiladi:

1. **Oddiy IP kamera** — HTTP-MJPEG oqim (`/cam1.mjpg`). Koridor sun'iy chiziladi:
   eshiklar, pol, shovqin, yorug'likning sekin o'zgarishi. Boshqaruv buyrug'i bilan
   «odam» eshik oldida turadi yoki yonidan o'tadi, kamera yopiladi yoki o'chadi.
   Edge agent uni `rtsp` manbasi orqali o'qiydi (OpenCV/FFmpeg, RTSP bilan bir xil yo'l).
2. **Hikvision qurilma** — ISAPI: Digest auth, `alertStream` (multipart XML),
   kanallar holati, kadr. Boshqaruv buyrug'i bilan intrusion/regionEntrance/
   shelteralarm hodisalari yuboriladi.

Ishga tushirish:
    python -m tools.camsim --port 8601 --cameras 2

Boshqaruv (JSON POST /control):
    {"cam": 1, "action": "stand", "door": 2, "seconds": 4}   # eshik oldida turish
    {"cam": 1, "action": "pass"}                               # koridordan o'tib ketish
    {"cam": 1, "action": "cover", "seconds": 20}               # kamerani yopish
    {"cam": 1, "action": "offline", "seconds": 20}             # kamera o'chdi
    {"cam": 1, "action": "lights", "level": 0.5, "seconds": 10} # chiroq o'chdi (yorqinlik 50%)
    {"hik": true, "channel": 2, "type": "fielddetection", "region": 3}
    {"hik": true, "channel": 2, "type": "shelteralarm", "state": "active"}

GET /zones.json — har bir eshik zonasining nisbiy ko'pburchagi (panelga yuklash uchun).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import secrets
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

W, H, FPS = 640, 360, 10
DOORS_PER_SIDE = 3


# ------------------------------------------------------------------ scene
def corridor_layout() -> list[dict]:
    """Perspektivali koridor: chapda va o'ngda 3 tadan eshik. Eshik oldidagi pol — zona."""
    doors = []
    # Uzoqlashgan sari eshiklar kichrayadi (x — markazga, y — ufqqa yaqinlashadi)
    for side in ("L", "R"):
        for i in range(DOORS_PER_SIDE):
            depth = i / DOORS_PER_SIDE  # 0 — yaqin, ~0.67 — uzoq
            scale = 1 - 0.55 * depth
            w = 0.11 * scale
            h = 0.55 * scale
            top = 0.18 + 0.12 * depth
            if side == "L":
                x0 = 0.02 + 0.45 * depth
            else:
                x0 = 0.98 - w - 0.45 * depth
            bottom = top + h
            # Eshik oldidagi pol (zona) — eshik ostidagi trapetsiya
            inward = 0.06 * scale if side == "L" else -0.06 * scale
            zone = [
                [x0, bottom], [x0 + w, bottom],
                [x0 + w + inward, min(0.98, bottom + 0.12 * scale)], [x0 + inward, min(0.98, bottom + 0.12 * scale)],
            ]
            doors.append({"door": len(doors) + 1, "side": side, "rect": [x0, top, w, h], "zone": zone, "scale": scale})
    return doors


DOORS = corridor_layout()


class Actor:
    def __init__(self, kind: str, door: int | None, start: float, seconds: float):
        self.kind, self.door, self.start, self.seconds = kind, door, start, seconds

    def position(self, t: float):
        """(x, y, scale) — nisbiy koordinata, yoki None (sahnada yo'q)."""
        dt = t - self.start
        if dt < 0 or dt > self.seconds:
            return None
        if self.kind == "stand":
            d = DOORS[self.door - 1]
            x0, top, w, h = d["rect"]
            zx = sum(p[0] for p in d["zone"]) / 4
            zy = max(p[1] for p in d["zone"]) - 0.01
            # yaqinlashadi (0.6 s), turadi (kalit/karta), eshikka kiradi (oxirgi 0.6 s)
            jitter = 0.004 * np.sin(dt * 7)
            return zx + jitter, zy, d["scale"]
        # «pass»: koridor bo'ylab kameradan uzoqqa yurib ketadi
        p = dt / self.seconds
        x = 0.5 + 0.05 * np.sin(p * 3)
        y = 0.98 - 0.45 * p
        return x, y, 1 - 0.6 * p


class Camera:
    def __init__(self, cam_id: int, seed: int):
        self.id = cam_id
        self.rng = np.random.default_rng(seed)
        self.actors: list[Actor] = []
        self.covered_until = 0.0
        self.offline_until = 0.0
        self.dim_until = 0.0
        self.dim_level = 1.0
        self.base = self._background()
        self.lock = threading.Lock()

    def _background(self) -> np.ndarray:
        img = np.zeros((H, W, 3), np.float32)
        yy = np.linspace(0, 1, H)[:, None]
        xx = np.linspace(0, 1, W)[None, :]
        img[:] = (205, 200, 195)  # devorlar
        floor = (yy > 0.45 + 0.0 * xx) & (np.abs(xx - 0.5) < (yy - 0.3) * 0.95)
        img[floor] = (125, 130, 140)
        img[:int(H * 0.12)] = (225, 225, 225)  # shift
        for d in DOORS:
            x0, top, w, h = d["rect"]
            img[int(top * H):int((top + h) * H), int(x0 * W):int((x0 + w) * W)] = (60, 80, 120)
            # eshik raqami tabelkasi
            img[int((top + 0.03) * H):int((top + 0.07) * H), int((x0 + w * 0.3) * W):int((x0 + w * 0.7) * W)] = (230, 230, 230)
        return img

    @property
    def online(self) -> bool:
        return time.time() >= self.offline_until

    def act(self, cmd: dict) -> None:
        now = time.time()
        with self.lock:
            a = cmd["action"]
            if a == "stand":
                self.actors.append(Actor("stand", int(cmd["door"]), now, float(cmd.get("seconds", 4))))
            elif a == "pass":
                self.actors.append(Actor("pass", None, now, float(cmd.get("seconds", 3))))
            elif a == "cover":
                self.covered_until = now + float(cmd.get("seconds", 20))
            elif a == "lights":  # chiroq o'chdi / tungi rejim (yorqinlik keskin o'zgaradi)
                self.dim_level = float(cmd.get("level", 0.5))
                self.dim_until = now + float(cmd.get("seconds", 10))
            elif a == "offline":
                self.offline_until = now + float(cmd.get("seconds", 20))
            self.actors = [x for x in self.actors if now - x.start < x.seconds + 1]

    def frame(self) -> np.ndarray:
        t = time.time()
        if t < self.covered_until:
            dark = np.full((H, W, 3), 8, np.uint8)
            return (dark + self.rng.integers(0, 3, size=dark.shape)).astype(np.uint8)
        # yorug'likning sekin o'zgarishi (±4%) va sensor shovqini
        light = (1 + 0.04 * np.sin(t / 20)) * (self.dim_level if t < self.dim_until else 1.0)
        img = self.base * light + self.rng.normal(0, 4, size=self.base.shape)
        with self.lock:
            actors = list(self.actors)
        for a in actors:
            pos = a.position(t)
            if pos is None:
                continue
            x, y, s = pos
            bw, bh = 0.045 * s, 0.33 * s
            x0, x1 = int((x - bw / 2) * W), int((x + bw / 2) * W)
            y0, y1 = int((y - bh) * H), int(y * H)
            img[max(0, y0):y1, max(0, x0):x1] = (35, 40, 55)  # tana
            r = int(bw * W * 0.45)
            cy, cx = max(0, y0 - r), (x0 + x1) // 2
            img[max(0, cy - r):cy + r, max(0, cx - r):cx + r] = (90, 110, 150)  # bosh
        return img.clip(0, 255).astype(np.uint8)

    def jpeg(self) -> bytes:
        import cv2

        ok, buf = cv2.imencode(".jpg", self.frame(), [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes()


# ------------------------------------------------------------------ fake Hikvision (ISAPI)
class FakeHikvision:
    REALM = "IP Camera(SIM)"

    def __init__(self, user: str, password: str, channels: list[int]):
        self.user, self.password = user, password
        self.channels = {c: True for c in channels}
        self.subscribers: list[queue.Queue] = []
        self.lock = threading.Lock()

    # --- Digest auth (RFC 2617, qop=auth)
    def challenge(self) -> str:
        return f'Digest realm="{self.REALM}", qop="auth", nonce="{secrets.token_hex(16)}", algorithm=MD5'

    def check(self, header: str | None, method: str, uri: str) -> bool:
        if not header or not header.startswith("Digest "):
            return False
        parts = {}
        for item in header[7:].split(","):
            if "=" in item:
                k, v = item.strip().split("=", 1)
                parts[k] = v.strip('"')
        ha1 = hashlib.md5(f"{self.user}:{self.REALM}:{self.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"{method}:{parts.get('uri', uri)}".encode()).hexdigest()
        if parts.get("qop"):
            expected = hashlib.md5(
                f"{ha1}:{parts['nonce']}:{parts['nc']}:{parts['cnonce']}:{parts['qop']}:{ha2}".encode()
            ).hexdigest()
        else:
            expected = hashlib.md5(f"{ha1}:{parts.get('nonce')}:{ha2}".encode()).hexdigest()
        return parts.get("username") == self.user and secrets.compare_digest(expected, parts.get("response", ""))

    def emit(self, channel: int, etype: str, region: int = 1, state: str = "active") -> None:
        now = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        xml = (
            '<EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">'
            f"<ipAddress>127.0.0.1</ipAddress><channelID>{channel}</channelID><dateTime>{now}</dateTime>"
            f"<activePostCount>1</activePostCount><eventType>{etype}</eventType><eventState>{state}</eventState>"
            f"<eventDescription>{etype} alarm</eventDescription><DetectionRegionList><DetectionRegionEntry>"
            f"<regionID>{region}</regionID><detectionTarget>human</detectionTarget></DetectionRegionEntry>"
            "</DetectionRegionList></EventNotificationAlert>"
        )
        with self.lock:
            for q in self.subscribers:
                q.put(xml)

    def status_xml(self) -> str:
        items = "".join(
            f"<InputProxyChannelStatus><id>{c}</id><online>{'true' if ok else 'false'}</online></InputProxyChannelStatus>"
            for c, ok in self.channels.items()
        )
        return f'<?xml version="1.0"?><InputProxyChannelStatusList version="2.0">{items}</InputProxyChannelStatusList>'


# ------------------------------------------------------------------ HTTP
def make_handler(cams: dict[int, Camera], hik: FakeHikvision):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # jim
            pass

        def _send(self, code: int, body: bytes, ctype: str = "application/json", extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _auth(self) -> bool:
            if hik.check(self.headers.get("Authorization"), self.command, self.path):
                return True
            self._send(401, b"", "text/plain", {"WWW-Authenticate": hik.challenge()})
            return False

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/":
                return self._send(200, control_page(cams).encode(), "text/html; charset=utf-8")
            if p == "/zones.json":
                return self._send(200, json.dumps({"doors": DOORS, "width": W, "height": H}).encode())
            if p.startswith("/cam") and (p.endswith(".mjpg") or p.endswith(".jpg")):
                cam = cams.get(int(p[4:].split(".")[0]))
                if cam is None or not cam.online:
                    return self._send(503, b"offline", "text/plain")
                if p.endswith(".jpg"):
                    return self._send(200, cam.jpeg(), "image/jpeg")
                return self._mjpeg(cam)
            if p.startswith("/ISAPI/"):
                if not self._auth():
                    return
                if p == "/ISAPI/Event/notification/alertStream":
                    return self._alert_stream()
                if p == "/ISAPI/ContentMgmt/InputProxy/channels/status":
                    return self._send(200, hik.status_xml().encode(), "application/xml")
                if p.startswith("/ISAPI/Streaming/channels/") and p.endswith("/picture"):
                    ch = int(p.split("/")[4]) // 100
                    cam = cams.get(ch) or next(iter(cams.values()))
                    return self._send(200, cam.jpeg(), "image/jpeg")
            self._send(404, b"{}")

        def _mjpeg(self, cam: Camera):
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while cam.online:
                    jpg = cam.jpeg()
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n")
                    self.wfile.write(jpg + b"\r\n")
                    self.wfile.flush()
                    time.sleep(1 / FPS)
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True

        def _alert_stream(self):
            q: queue.Queue = queue.Queue()
            with hik.lock:
                hik.subscribers.append(q)
            self.send_response(200)
            self.send_header("Content-Type", "multipart/mixed; boundary=boundary")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while True:
                    try:
                        xml = q.get(timeout=10)
                    except queue.Empty:
                        # haqiqiy NVR kabi davriy «videoloss inactive» (yurak urishi)
                        xml = ('<EventNotificationAlert version="2.0"><channelID>1</channelID><eventType>videoloss</eventType>'
                               "<eventState>inactive</eventState></EventNotificationAlert>")
                    body = xml.encode()
                    self.wfile.write(b"--boundary\r\nContent-Type: application/xml; charset=\"UTF-8\"\r\nContent-Length: "
                                     + str(len(body)).encode() + b"\r\n\r\n" + body + b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with hik.lock:
                    hik.subscribers.remove(q)
            self.close_connection = True

        def do_POST(self):
            if self.path != "/control":
                return self._send(404, b"{}")
            cmd = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if cmd.get("hik"):
                if "online" in cmd:
                    hik.channels[int(cmd["channel"])] = bool(cmd["online"])
                else:
                    hik.emit(int(cmd["channel"]), cmd["type"], int(cmd.get("region", 1)), cmd.get("state", "active"))
            else:
                cams[int(cmd["cam"])].act(cmd)
            self._send(200, b'{"ok": true}')

    return Handler


def control_page(cams: dict) -> str:
    """Virtual kamerani boshqarish sahifasi: jonli tasvir va tugmalar."""
    rooms = getattr(control_page, "rooms", {})
    blocks = []
    for cid in cams:
        doors = "".join(
            f'<button onclick="act({{cam:{cid},action:\'stand\',door:{d["door"]},seconds:5}})">'
            f'🚶 {rooms.get((cid, d["door"]), str(d["door"]) + "-eshik")} oldida turish</button>'
            for d in DOORS
        )
        blocks.append(f"""
<section><h2>{cid}-kamera</h2><img src="/cam{cid}.mjpg" alt="kamera {cid}">
<div class="b">{doors}</div>
<div class="b"><button onclick="act({{cam:{cid},action:'pass',seconds:3}})">🚶‍➡️ Koridordan o'tib ketish</button>
<button onclick="act({{cam:{cid},action:'lights',level:0.45,seconds:8}})">💡 Chiroq o'chdi (8 s)</button>
<button class="w" onclick="act({{cam:{cid},action:'cover',seconds:20}})">🙈 Kamerani yopish (20 s)</button>
<button class="w" onclick="act({{cam:{cid},action:'offline',seconds:20}})">🔌 Kamera o'chdi (20 s)</button></div></section>""")
    return f"""<!doctype html><html lang="uz"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Virtual kameralar</title><style>
body{{font-family:system-ui,sans-serif;margin:16px;background:#f3f5f8;color:#1c2530}}h1{{font-size:20px}}
section{{background:#fff;border:1px solid #e1e6ec;border-radius:10px;padding:12px;margin-bottom:16px}}
img{{max-width:100%;border-radius:6px;display:block}}.b{{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}}
button{{padding:8px 10px;border:0;border-radius:8px;background:#0b6bcb;color:#fff;cursor:pointer}}button.w{{background:#a15c00}}
#log{{color:#13804b;min-height:1.2em}}</style></head><body>
<h1>🎥 Virtual kameralar (sinov uchun)</h1>
<p>Tugmani bosing — «odam» kadrda harakat qiladi, edge agent buni aniqlab serverga yuboradi. Natijani panelda ko'ring: Bandlik, Ogohlantirishlar, Kameralar.</p>
<p id="log"></p>{"".join(blocks)}
<script>function act(c){{fetch('/control',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(c)}})
.then(()=>{{document.getElementById('log').textContent='✓ '+c.action+' — '+new Date().toLocaleTimeString()}})}}</script>
</body></html>"""


def serve(port: int, cameras: int, hik_user: str = "admin", hik_password: str = "Sim12345",
          host: str = "127.0.0.1") -> ThreadingHTTPServer:
    cams = {i: Camera(i, seed=i) for i in range(1, cameras + 1)}
    hik = FakeHikvision(hik_user, hik_password, list(cams))
    srv = ThreadingHTTPServer((host, port), make_handler(cams, hik))
    srv.daemon_threads = True
    srv.cams, srv.hik = cams, hik  # type: ignore[attr-defined]
    return srv


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("CAMSIM_PORT", 8601)))
    ap.add_argument("--cameras", type=int, default=2)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()
    srv = serve(a.port, a.cameras, host=a.host)
    # Boshqaruv sahifasidagi yozuvlar: n-kamera → n-qavat (101..106, 201..206) — run_local shunday bog'laydi
    control_page.rooms = {(c, d["door"]): f"{c}0{d['door']}-xona" for c in range(1, a.cameras + 1) for d in DOORS}
    print(f"Kamera simulyatori: http://127.0.0.1:{a.port}/cam1.mjpg … , Hikvision ISAPI: admin / Sim12345", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
