"""Istalgan IP kamera (RTSP): video tahlil mini-PC'da.

Kameradan faqat RTSP oqim kerak (deyarli barcha arzon kameralarda bor). Kichik
substream (masalan 640×360) olinadi, sekundiga ~5 kadr tahlil qilinadi. Eshik
zonalari panelda kamera surati ustida chiziladi va serverdan olinadi.

Talab: `pip install opencv-python-headless numpy`. Intel N100 kabi oddiy mini-PC
4–8 ta koridor kamerasini bemalol tortadi.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from edge.motion import MotionConfig, MotionDetector

log = logging.getLogger("edge.rtsp")

ANALYSIS_WIDTH = 320


class RtspSource:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.channel = int(cfg["channel"])
        self.url = cfg["url"]
        self.key = f"rtsp:{self.channel}"
        self.fps = float(cfg.get("fps", 5))
        self.motion_cfg = MotionConfig(
            **{k: cfg[k] for k in ("dwell_seconds", "min_fraction", "pixel_threshold") if k in cfg}
        )
        self.stop = threading.Event()

    async def run(self, sink) -> None:
        import asyncio

        loop = asyncio.get_running_loop()

        class ThreadSink:
            """Thread ichidan agentga xavfsiz murojaat (sqlite bitta threadda qoladi)."""

            def __getattr__(self, name):
                fn = getattr(sink, name)
                return lambda *a: loop.call_soon_threadsafe(fn, *a)

            def zones_for(self, channel):
                return sink.zones_for(channel)

        await asyncio.to_thread(self._loop, ThreadSink())

    def _loop(self, sink) -> None:
        import cv2  # noqa: PLC0415 — faqat rtsp manbasi uchun kerak

        backoff = 2
        while not self.stop.is_set():
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                sink.source_down(self.key, [self.channel])
                sink.set_online(self.channel, False)
                log.warning("RTSP ochilmadi (kanal %s), %s s dan keyin", self.channel, backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            backoff = 2
            sink.source_up(self.key, [self.channel])
            sink.set_online(self.channel, True)
            log.info("RTSP ulandi: kanal %s", self.channel)
            detector, zones_sig, last_ref, last_t, fails = None, None, 0.0, 0.0, 0
            while not self.stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    fails += 1
                    if fails > 25:
                        break
                    continue
                fails = 0
                if self.cfg.get("file_time"):  # sinov: video fayl vaqti bo'yicha
                    now = float(self.cfg.get("file_start", 0)) + cap.get(cv2.CAP_PROP_POS_MSEC) / 1000.0
                else:
                    now = time.time()
                if now - last_t < 1.0 / self.fps:
                    continue
                last_t = now
                h, w = frame.shape[:2]
                small = cv2.resize(frame, (ANALYSIS_WIDTH, max(1, int(h * ANALYSIS_WIDTH / w))))
                gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
                zones = sink.zones_for(self.channel)
                sig = repr(zones)
                if detector is None or sig != zones_sig:
                    detector = MotionDetector(zones, gray.shape[0], gray.shape[1], self.motion_cfg)
                    zones_sig = sig
                if now - last_ref > 1800:  # panel uchun ma'lumotnoma kadr
                    ok_j, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                    if ok_j:
                        sink.upload_snapshot(self.channel, jpg.tobytes())
                    last_ref = now
                for d in detector.process(gray, now):
                    ev = {
                        "channel": self.channel,
                        "region_id": d.get("region_id", 0),
                        "event_type": d["type"],
                        "vendor_type": "edge-motion",
                        "occurred_at": datetime.fromtimestamp(d["t"], timezone.utc).isoformat(),
                    }
                    if "state" in d:
                        ev["state"] = d["state"]
                    if "reason" in d:
                        ev["reason"] = d["reason"]
                    if d["type"] in ("dwell", "enter"):
                        ok_j, jpg = cv2.imencode(".jpg", small if w > 1280 else frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                        if ok_j:
                            ev["snapshot"] = jpg.tobytes()
                    sink.emit(ev)
            cap.release()
            sink.source_down(self.key, [self.channel])
            sink.set_online(self.channel, False)
            log.warning("RTSP uzildi: kanal %s", self.channel)
