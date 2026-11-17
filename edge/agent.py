"""Edge agent: mehmonxona ichidagi kichik kompyuterda (mini-PC / Raspberry Pi) ishlaydi.

Vazifasi:
  1. Hikvision NVR'ning ISAPI hodisalar oqimiga ulanadi
     (GET /ISAPI/Event/notification/alertStream, Digest auth).
  2. «hududga kirish» (regionEntrance), «chiziq kesish» (linedetection) va
     «bostirib kirish» (fielddetection) hodisalarini ajratib oladi; AcuSense
     kameralari bergan nishon turini (human/vehicle) ham uzatadi.
  3. Kamera sog'lig'i: «videoloss» va «shelteralarm» (kamera yopildi) hodisalari
     faqat holat o'zgarganda yuboriladi; har daqiqada kanallar holati
     (/ISAPI/ContentMgmt/InputProxy/channels/status) so'raladi.
  4. NVR oqimi uzilgan davrlarni (va agentning o'zi ishlamagan davrni) qayd etib,
     serverga yuboradi — shu davrdagi kirish oynalari uchun noto'g'ri R1 chiqmaydi.
  5. Har bir hodisaga edge (NTP) qabul vaqtini qo'shadi — NVR soati adashsa ham
     server to'g'ri vaqtni ishlatadi.
  6. Ixtiyoriy: hodisa paytidagi kadrni (snapshot) oladi.
  7. Hodisalarni lokal SQLite navbatiga yozadi va serverga HMAC imzo bilan
     paketlab yuboradi. Internet uzilsa — navbatda saqlanadi, ulanganda
     yuboriladi (TT 6.6, 16-bo'lim 12-sinov).
  8. Har daqiqada heartbeat yuboradi (FT-36: 15 daqiqa aloqa yo'q — R12).

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
VERSION = "1.1"

ALERT_RE = re.compile(rb"<EventNotificationAlert[\s\S]*?</EventNotificationAlert>")
INTERESTING = {"regionentrance", "linedetection", "fielddetection", "regionexiting"}
HEALTH = {"videoloss", "shelteralarm"}
CHANNEL_STATUS_RE = re.compile(
    rb"<InputProxyChannelStatus[^>]*>[\s\S]*?<id>(\d+)</id>[\s\S]*?<online>(true|false)</online>[\s\S]*?</InputProxyChannelStatus>"
)


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
    state = flat.get("eventState", "active").lower()
    is_health = etype.lower() in HEALTH
    if not is_health and (etype.lower() not in INTERESTING or state != "active"):
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
    out = {
        "channel": channel_i,
        "region_id": regions[0] if regions else (0 if is_health else 1),
        "event_type": etype,
        "occurred_at": occurred.isoformat(),
    }
    if is_health:
        out["state"] = state
    target = flat.get("targetType") or flat.get("detectionTarget")
    if target:
        out["target"] = target.lower()
    return out


def parse_channel_status(xml_bytes: bytes) -> dict[str, bool]:
    """/ISAPI/ContentMgmt/InputProxy/channels/status javobidan {kanal: onlayn}."""
    return {m.group(1).decode(): m.group(2) == b"true" for m in CHANNEL_STATUS_RE.finditer(xml_bytes)}


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
        self.conn.execute("CREATE TABLE IF NOT EXISTS gaps (start TEXT PRIMARY KEY, end TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
        self.conn.commit()

    # --- kuzatuv uzilishlari (NVR oqimi yo'q yoki agent ishlamagan davrlar)
    def gap_open(self, start: str) -> None:
        self.conn.execute("INSERT OR IGNORE INTO gaps (start, end) VALUES (?, NULL)", (start,))
        self.conn.commit()

    def gap_close(self, end: str) -> None:
        self.conn.execute("UPDATE gaps SET end = ? WHERE end IS NULL", (end,))
        self.conn.commit()

    def gaps(self, since_hours: int = 48) -> list[dict]:
        cutoff = datetime.fromtimestamp(time.time() - since_hours * 3600, timezone.utc).isoformat()
        rows = self.conn.execute(
            "SELECT start, end FROM gaps WHERE end IS NULL OR end >= ? ORDER BY start", (cutoff,)
        ).fetchall()
        return [{"start": s, "end": e} for s, e in rows]

    def meta_get(self, k: str) -> str | None:
        row = self.conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
        return row[0] if row else None

    def meta_set(self, k: str, v: str) -> None:
        self.conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, v))
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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        self.channels: dict[str, bool] = {}
        self.health_state: dict[tuple[int, str], str] = {}
        # Agent o'chib turgan davr ham kuzatuv uzilishi hisoblanadi
        last_alive = self.queue.meta_get("last_alive")
        self.queue.gap_open(last_alive or now_iso())
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
                        self.queue.gap_close(now_iso())
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
                                if "state" in ev:
                                    # videoloss/shelteralarm: NVR har necha soniyada «inactive» yuboradi —
                                    # faqat holat o'zgarganini uzatamiz
                                    key = (ev["channel"], ev["event_type"].lower())
                                    if self.health_state.get(key, "inactive") == ev["state"]:
                                        continue
                                    self.health_state[key] = ev["state"]
                                ev["edge_received_at"] = now_iso()
                                ev["id"] = f"{ev['channel']}-{ev['region_id']}-{ev['occurred_at']}-{uuid.uuid4().hex[:6]}"
                                if "state" not in ev and ev["event_type"].lower() != "linedetection":
                                    ev["snapshot_b64"] = await self.snapshot(client, ev["channel"])
                                self.queue.put(ev)
                                log.info("Hodisa: kanal %s zona %s %s", ev["channel"], ev["region_id"], ev["event_type"])
                except (httpx.HTTPError, OSError) as e:
                    if self.nvr_ok:
                        self.queue.gap_open(now_iso())
                    self.nvr_ok = False
                    log.warning("NVR bilan aloqa yo'q: %s (qayta urinish %s s)", e, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    async def push(self) -> None:
        """Navbatdagi hodisalarni serverga yuborish (har 2 soniyada)."""
        while True:
            batch = self.queue.peek(self.cfg.batch_size)
            if batch:
                remaining = max(0, self.queue.size() - len(batch))
                body = json.dumps({"events": batch, "nvr_ok": self.nvr_ok, "queue": remaining}).encode()
                try:
                    r = await self.server.post("/api/edge/events", content=body, headers=sign(self.cfg.edge_secret, body))
                    if r.status_code == 200:
                        self.queue.ack([e["id"] for e in batch])
                        continue
                    log.error("Server rad etdi: %s %s", r.status_code, r.text[:200])
                except httpx.HTTPError as e:
                    log.warning("Serverga ulanib bo'lmadi: %s (navbat: %s)", e, self.queue.size())
            await asyncio.sleep(2)

    async def poll_channels(self) -> None:
        """Har daqiqada NVR kanallari holati (kamera oflayn — R12)."""
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                try:
                    r = await client.get(
                        f"{self.nvr_base}/ISAPI/ContentMgmt/InputProxy/channels/status", auth=self.nvr_auth
                    )
                    if r.status_code == 200:
                        status = parse_channel_status(r.content)
                        if status:
                            self.channels = status
                except httpx.HTTPError as e:
                    log.warning("Kanal holatini olib bo'lmadi: %s", e)
                await asyncio.sleep(60)

    async def heartbeat(self) -> None:
        while True:
            if self.nvr_ok:
                self.queue.meta_set("last_alive", now_iso())
            body = json.dumps(
                {
                    "nvr_ok": self.nvr_ok,
                    "queue": self.queue.size(),
                    "version": VERSION,
                    "channels": self.channels,
                    "gaps": self.queue.gaps(),
                }
            ).encode()
            try:
                await self.server.post("/api/edge/heartbeat", content=body, headers=sign(self.cfg.edge_secret, body))
            except httpx.HTTPError as e:
                log.warning("Heartbeat yuborilmadi: %s", e)
            await asyncio.sleep(self.cfg.heartbeat_seconds)

    async def run(self) -> None:
        await asyncio.gather(self.listen_nvr(), self.push(), self.heartbeat(), self.poll_channels())


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="edge/config.toml")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(Agent(Config.load(args.config)).run())


if __name__ == "__main__":
    main()
