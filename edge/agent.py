"""Edge agent: mehmonxona ichidagi kichik kompyuterda (mini-PC / Raspberry Pi) ishlaydi.

Vazifasi:
  1. Hikvision NVR'ning ISAPI hodisalar oqimiga ulanadi
     (GET /ISAPI/Event/notification/alertStream, Digest auth).
  2. «hududga kirish» (regionEntrance), «chiziq kesish» (linedetection) va
     «bostirib kirish» (fielddetection) hodisalarini ajratib oladi.
  3. Ixtiyoriy: hodisa paytidagi kadrni (snapshot) oladi.
  4. Hodisalarni lokal SQLite navbatiga yozadi va serverga HMAC imzo bilan
     paketlab yuboradi. Internet uzilsa — navbatda saqlanadi, ulanganda
     yuboriladi (TT 6.6, 16-bo'lim 12-sinov).
  5. Har daqiqada heartbeat yuboradi (FT-36: 15 daqiqa aloqa yo'q — R12).

NVR internetga ochilmaydi (TT 14-bo'lim): agent faqat lokal tarmoqda NVR'ga
ulanadi va serverga chiquvchi HTTPS orqali ishlaydi.

Ishga tushirish:
    python -m edge.agent --config edge/config.toml
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import sqlite3
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import httpx

log = logging.getLogger("edge")
VERSION = "1.0"

ALERT_RE = re.compile(rb"<EventNotificationAlert[\s\S]*?</EventNotificationAlert>")
INTERESTING = {"regionentrance", "linedetection", "fielddetection", "regionexiting"}


@dataclass
class Config:
    server_url: str
    edge_secret: str
    nvr_host: str
    nvr_user: str
    nvr_password: str
    nvr_scheme: str = "http"
    queue_path: str = "edge/edge.queue.db"
    snapshots: bool = True
    batch_size: int = 50
    heartbeat_seconds: int = 60
    channels: list[int] = field(default_factory=list)  # bo'sh — barchasi
    verify_tls: bool = True

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls(**data)


# ------------------------------------------------------------------ parsing
def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def parse_alert(xml_bytes: bytes) -> dict | None:
    """EventNotificationAlert XML ni lug'atga aylantiradi. Keraksiz hodisa — None."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    flat: dict[str, str] = {}
    regions: list[int] = []
    for el in root.iter():
        name = _strip_ns(el.tag)
        text = (el.text or "").strip()
        if name == "regionID" and text.isdigit():
            regions.append(int(text))
        elif text and name not in flat:
            flat[name] = text
    etype = flat.get("eventType", "")
    state = flat.get("eventState", "active")
    if etype.lower() not in INTERESTING or state.lower() != "active":
        return None
    channel = flat.get("channelID") or flat.get("dynChannelID") or "0"
    try:
        channel_i = int(channel)
    except ValueError:
        return None
    dt = flat.get("dateTime")
    try:
        occurred = datetime.fromisoformat(dt) if dt else datetime.now(timezone.utc)
    except ValueError:
        occurred = datetime.now(timezone.utc)
    if occurred.tzinfo is None:
        occurred = occurred.astimezone()
    return {
        "channel": channel_i,
        "region_id": regions[0] if regions else 1,
        "event_type": etype,
        "occurred_at": occurred.isoformat(),
    }


def extract_alerts(buffer: bytearray) -> list[bytes]:
    """Multipart oqim buferidan to'liq XML hujjatlarni ajratib, buferni qisqartiradi."""
    out = []
    last_end = 0
    for m in ALERT_RE.finditer(buffer):
        out.append(bytes(m.group(0)))
        last_end = m.end()
    if last_end:
        del buffer[:last_end]
    elif len(buffer) > 1_000_000:  # himoya: noma'lum ma'lumot to'planib qolmasin
        del buffer[:-10_000]
    return out


# ------------------------------------------------------------------ queue
class Queue:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS q (id TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL)"
        )
        self.conn.commit()

    def put(self, event: dict) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO q (id, payload, created) VALUES (?, ?, ?)",
            (event["id"], json.dumps(event), time.time()),
        )
        self.conn.commit()

    def peek(self, n: int) -> list[dict]:
        rows = self.conn.execute("SELECT payload FROM q ORDER BY created LIMIT ?", (n,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def ack(self, ids: list[str]) -> None:
        self.conn.executemany("DELETE FROM q WHERE id = ?", [(i,) for i in ids])
        self.conn.commit()

    def size(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM q").fetchone()[0]


# ------------------------------------------------------------------ server client
def sign(secret: str, body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"x-edge-timestamp": ts, "x-edge-signature": sig, "content-type": "application/json"}


class Agent:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.queue = Queue(cfg.queue_path)
        self.nvr_ok = False
        self.nvr_auth = httpx.DigestAuth(cfg.nvr_user, cfg.nvr_password)
        self.nvr_base = f"{cfg.nvr_scheme}://{cfg.nvr_host}"
        self.server = httpx.AsyncClient(base_url=cfg.server_url.rstrip("/"), timeout=20, verify=cfg.verify_tls)

    async def snapshot(self, client: httpx.AsyncClient, channel: int) -> str | None:
        if not self.cfg.snapshots:
            return None
        try:
            r = await client.get(f"{self.nvr_base}/ISAPI/Streaming/channels/{channel}01/picture", auth=self.nvr_auth, timeout=8)
            if r.status_code == 200 and r.content[:2] == b"\xff\xd8" and len(r.content) < 1_500_000:
                return base64.b64encode(r.content).decode()
        except httpx.HTTPError as e:
            log.warning("Snapshot olinmadi (kanal %s): %s", channel, e)
        return None

    async def listen_nvr(self) -> None:
        """NVR alertStream'ga doimiy ulanish; uzilsa — qayta ulanadi."""
        backoff = 2
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=120)) as client:
            while True:
                try:
                    async with client.stream(
                        "GET", f"{self.nvr_base}/ISAPI/Event/notification/alertStream", auth=self.nvr_auth
                    ) as resp:
                        if resp.status_code != 200:
                            raise httpx.HTTPError(f"HTTP {resp.status_code}")
                        self.nvr_ok = True
                        backoff = 2
                        log.info("NVR oqimiga ulandi")
                        buf = bytearray()
                        async for chunk in resp.aiter_bytes():
                            buf.extend(chunk)
                            for xml in extract_alerts(buf):
                                ev = parse_alert(xml)
                                if ev is None:
                                    continue
                                if self.cfg.channels and ev["channel"] not in self.cfg.channels:
                                    continue
                                ev["id"] = f"{ev['channel']}-{ev['region_id']}-{ev['occurred_at']}-{uuid.uuid4().hex[:6]}"
                                if ev["event_type"].lower() != "linedetection":
                                    ev["snapshot_b64"] = await self.snapshot(client, ev["channel"])
                                self.queue.put(ev)
                                log.info("Hodisa: kanal %s zona %s %s", ev["channel"], ev["region_id"], ev["event_type"])
                except (httpx.HTTPError, OSError) as e:
                    self.nvr_ok = False
                    log.warning("NVR bilan aloqa yo'q: %s (qayta urinish %s s)", e, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    async def push(self) -> None:
        """Navbatdagi hodisalarni serverga yuborish (har 2 soniyada)."""
        while True:
            batch = self.queue.peek(self.cfg.batch_size)
            if batch:
                body = json.dumps({"events": batch, "nvr_ok": self.nvr_ok, "queue": self.queue.size()}).encode()
                try:
                    r = await self.server.post("/api/edge/events", content=body, headers=sign(self.cfg.edge_secret, body))
                    if r.status_code == 200:
                        self.queue.ack([e["id"] for e in batch])
                        continue
                    log.error("Server rad etdi: %s %s", r.status_code, r.text[:200])
                except httpx.HTTPError as e:
                    log.warning("Serverga ulanib bo'lmadi: %s (navbat: %s)", e, self.queue.size())
            await asyncio.sleep(2)

    async def heartbeat(self) -> None:
        while True:
            body = json.dumps({"nvr_ok": self.nvr_ok, "queue": self.queue.size(), "version": VERSION}).encode()
            try:
                await self.server.post("/api/edge/heartbeat", content=body, headers=sign(self.cfg.edge_secret, body))
            except httpx.HTTPError as e:
                log.warning("Heartbeat yuborilmadi: %s", e)
            await asyncio.sleep(self.cfg.heartbeat_seconds)

    async def run(self) -> None:
        await asyncio.gather(self.listen_nvr(), self.push(), self.heartbeat())


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="edge/config.toml")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(Agent(Config.load(args.config)).run())


if __name__ == "__main__":
    main()
