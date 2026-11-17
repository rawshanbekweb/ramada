"""RTSP manzilini tekshirish: ulanadimi, o'lcham, FPS, kadr saqlash.

    python -m edge.check "rtsp://user:parol@192.168.1.64:554/Streaming/Channels/102"
    python -m edge.check --hikvision 192.168.1.64 --user admin --password 'parol' --channel 1

Natijada `check_<kanal>.jpg` saqlanadi — kamera eshiklarni ko'rishini tekshiring.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from urllib.parse import quote


def hikvision_url(host: str, user: str, password: str, channel: int = 1, sub: bool = True, port: int = 554) -> str:
    """Hikvision RTSP manzili. Kamera: channel=1; NVR: kamera raqami (1, 2, 3...)."""
    stream = f"{channel}0{2 if sub else 1}"
    hostport = host if ":" in host else f"{host}:{port}"
    return f"rtsp://{quote(user, safe='')}:{quote(password, safe='')}@{hostport}/Streaming/Channels/{stream}"


def check(url: str, out: str, seconds: float = 5.0) -> int:
    os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000")
    import cv2

    safe = url.split("@")[-1]
    print(f"Ulanmoqda: ...@{safe}")
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print("❌ Ochilmadi: IP, port (554), login/parol, kanal raqamini tekshiring.")
        return 1
    t0, n, frame = time.time(), 0, None
    while time.time() - t0 < seconds:
        ok, f = cap.read()
        if ok:
            n, frame = n + 1, f
    cap.release()
    if frame is None:
        print("❌ Ulandi, lekin kadr kelmadi (kodek H.265+ bo'lishi mumkin — substream'ni H.264 qiling).")
        return 1
    h, w = frame.shape[:2]
    fps = n / seconds
    cv2.imwrite(out, frame)
    print(f"✅ Ishlayapti: {w}×{h}, ~{fps:.1f} kadr/s. Kadr saqlandi: {out}")
    if w > 1280:
        print("⚠️  Bu asosiy oqim (katta). Substream (…02) ishlating — mini-PC kam yuklanadi.")
    if fps < 4:
        print("⚠️  Kadrlar kam: kamerada substream FPS ni 10–15 qiling.")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="RTSP manzilini tekshirish")
    p.add_argument("url", nargs="?")
    p.add_argument("--hikvision", metavar="IP")
    p.add_argument("--user", default="admin")
    p.add_argument("--password", default="")
    p.add_argument("--channel", type=int, default=1)
    p.add_argument("--main", action="store_true", help="asosiy oqim (sukut bo'yicha substream)")
    a = p.parse_args()
    url = a.url or (hikvision_url(a.hikvision, a.user, a.password, a.channel, not a.main) if a.hikvision else None)
    if not url:
        p.error("URL yoki --hikvision IP kerak")
    if a.hikvision:
        print("RTSP manzil:", url.replace(quote(a.password, safe=""), "****") if a.password else url)
    sys.exit(check(url, f"check_{a.channel}.jpg"))


if __name__ == "__main__":
    main()
