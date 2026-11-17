"""Hikvision NVR yoki kamera: ISAPI alertStream (Digest auth).

GET /ISAPI/Event/notification/alertStream — multipart oqimda EventNotificationAlert XML.
Kanallar holati: /ISAPI/ContentMgmt/InputProxy/channels/status (faqat NVR).
Kadr: /ISAPI/Streaming/channels/{ch}01/picture.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import httpx

log = logging.getLogger("edge.hikvision")

ALERT_RE = re.compile(rb"<EventNotificationAlert[\s\S]*?</EventNotificationAlert>")
INTERESTING = {"regionentrance", "linedetection", "fielddetection", "regionexiting"}
HEALTH = {"videoloss", "shelteralarm"}
CHANNEL_STATUS_RE = re.compile(
    rb"<InputProxyChannelStatus[^>]*>[\s\S]*?<id>(\d+)</id>[\s\S]*?<online>(true|false)</online>[\s\S]*?</InputProxyChannelStatus>"
)
CANONICAL = {
    "fielddetection": "dwell",
    "regionentrance": "enter",
    "regionexiting": "enter",
    "linedetection": "line",
    "shelteralarm": "tamper",
    "videoloss": "videoloss",
}


def _strip_ns(tag: str) -> str:
    return tag.split("}", 1)[-1]


def parse_alert(xml_bytes: bytes) -> dict | None:
    """EventNotificationAlert XML ni lug'atga aylantiradi (Hikvision nomlari bilan). Keraksiz — None."""
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


def canonical(ev: dict) -> dict:
    out = dict(ev)
    out["vendor_type"] = ev["event_type"]
    out["event_type"] = CANONICAL.get(ev["event_type"].lower(), ev["event_type"])
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


class HikvisionSource:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.key = f"hikvision:{cfg['host']}"
        self.base = f"{cfg.get('scheme', 'http')}://{cfg['host']}"
        self.auth = httpx.DigestAuth(cfg["user"], cfg["password"])
        self.channels: list[int] = list(cfg.get("channels", []))
        self.offset = int(cfg.get("channel_offset", 0))  # bir nechta NVR bo'lsa kanal raqamlarini ajratish
        self.snapshots = bool(cfg.get("snapshots", True))

    async def snapshot(self, client: httpx.AsyncClient, channel: int) -> bytes | None:
        try:
            r = await client.get(f"{self.base}/ISAPI/Streaming/channels/{channel}01/picture", auth=self.auth, timeout=8)
            if r.status_code == 200 and r.content[:2] == b"\xff\xd8" and len(r.content) < 1_500_000:
                return r.content
        except httpx.HTTPError as e:
            log.warning("Snapshot olinmadi (kanal %s): %s", channel, e)
        return None

    async def listen(self, sink) -> None:
        backoff = 2
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=120)) as client:
            while True:
                try:
                    async with client.stream("GET", f"{self.base}/ISAPI/Event/notification/alertStream", auth=self.auth) as resp:
                        if resp.status_code != 200:
                            raise httpx.HTTPError(f"HTTP {resp.status_code}")
                        sink.source_up(self.key, self.channels or None)
                        backoff = 2
                        log.info("Hikvision oqimiga ulandi: %s", self.cfg["host"])
                        buf = bytearray()
                        async for chunk in resp.aiter_bytes():
                            buf.extend(chunk)
                            for xml in extract_alerts(buf):
                                ev = parse_alert(xml)
                                if ev is None:
                                    continue
                                if self.channels and ev["channel"] not in self.channels:
                                    continue
                                ev = canonical(ev)
                                if self.snapshots and ev["event_type"] in ("dwell", "enter"):
                                    jpeg = await self.snapshot(client, ev["channel"])
                                    if jpeg:
                                        ev["snapshot"] = jpeg
                                ev["channel"] += self.offset
                                sink.emit(ev)
                except (httpx.HTTPError, OSError) as e:
                    sink.source_down(self.key, [c + self.offset for c in self.channels] or None)
                    log.warning("Hikvision aloqasi yo'q: %s (qayta urinish %s s)", e, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    async def poll(self, sink) -> None:
        """Kanallar holati (har daqiqada) va panel uchun kadrlar (har 30 daqiqada)."""
        n = 0
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                try:
                    r = await client.get(f"{self.base}/ISAPI/ContentMgmt/InputProxy/channels/status", auth=self.auth)
                    if r.status_code == 200:
                        for ch, online in parse_channel_status(r.content).items():
                            sink.set_online(int(ch) + self.offset, online)
                except httpx.HTTPError as e:
                    log.warning("Kanal holatini olib bo'lmadi: %s", e)
                if self.snapshots and n % 30 == 0:
                    for ch in self.channels:
                        jpeg = await self.snapshot(client, ch)
                        if jpeg:
                            sink.upload_snapshot(ch + self.offset, jpeg)
                n += 1
                await asyncio.sleep(60)

    async def run(self, sink) -> None:
        await asyncio.gather(self.listen(sink), self.poll(sink))
