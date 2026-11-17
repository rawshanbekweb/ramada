"""Dahua NVR/kamera (va Dahua asosidagi OEM brendlar) — CGI hodisalar oqimi.

GET /cgi-bin/eventManager.cgi?action=attach&codes=[All]&heartbeat=5 (Digest auth).
Har bir qism matn ko'rinishida:

    Code=CrossRegionDetection;action=Start;index=0;data={ "Name" : "101", "ObjectType" : "Human", ... }

Eshik hududi (rule) nomini xona yoki hudud raqami bilan qo'ying («1», «2»…
yoki «101»): server shu raqamni zona sifatida xonaga bog'laydi.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone

import httpx

log = logging.getLogger("edge.dahua")

PART_RE = re.compile(r"Code=(?P<code>[A-Za-z]+);action=(?P<action>[A-Za-z]+);index=(?P<index>\d+)")
CANONICAL = {
    "crossregiondetection": "enter",
    "wanderdetection": "dwell",  # «aylanib yurish» (loitering) — odam joyida turdi
    "stayingdetection": "dwell",
    "crosslinedetection": "line",
    "videomotion": "motion",
    "smartmotionhuman": "motion",
    "videoblind": "tamper",
    "videoloss": "videoloss",
}
HEALTH = {"tamper", "videoloss"}


def _extract_json(text: str, start: int) -> tuple[dict | None, int]:
    """`data=` dan keyingi JSON obyektni qavslarni sanab ajratadi."""
    i = text.find("{", start)
    if i < 0:
        return None, start
    depth, in_str, esc = 0, False, False
    for j in range(i, len(text)):
        ch = text[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[i : j + 1]), j + 1
                except json.JSONDecodeError:
                    return None, j + 1
    return None, -1  # JSON hali to'liq kelmagan


def parse_stream(buffer: str) -> tuple[list[dict], str]:
    """Bufer → (kanonik hodisalar, qolgan bufer)."""
    events = []
    pos = 0
    while True:
        m = PART_RE.search(buffer, pos)
        if m is None:
            break
        end = m.end()
        data: dict | None = None
        if buffer.startswith(";data=", end):
            data, nxt = _extract_json(buffer, end)
            if nxt == -1:
                return events, buffer[m.start():]
            end = nxt
        pos = end
        ev = to_event(m.group("code"), m.group("action"), int(m.group("index")), data or {})
        if ev is not None:
            events.append(ev)
    rest = buffer[pos:]
    if len(rest) > 200_000:
        rest = rest[-2000:]
    return events, rest


def _region_from_name(name) -> int:
    digits = re.sub(r"\D", "", str(name or ""))
    return int(digits) if digits else 1


def to_event(code: str, action: str, index: int, data: dict) -> dict | None:
    etype = CANONICAL.get(code.lower())
    if etype is None:
        return None
    action = action.lower()
    out = {
        "channel": index + 1,
        "event_type": etype,
        "vendor_type": code,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
    }
    if etype in HEALTH:
        if action not in ("start", "stop"):
            return None
        out["state"] = "active" if action == "start" else "inactive"
        out["region_id"] = 0
        return out
    if action not in ("start", "pulse"):
        return None
    out["region_id"] = _region_from_name(data.get("Name")) if etype != "motion" else int(data.get("RegionID", 0) or 0)
    obj = data.get("ObjectType") or (data.get("Object") or {}).get("ObjectType")
    if obj:
        out["target"] = str(obj).lower()
    return out


class DahuaSource:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.key = f"dahua:{cfg['host']}"
        self.base = f"{cfg.get('scheme', 'http')}://{cfg['host']}"
        self.auth = httpx.DigestAuth(cfg["user"], cfg["password"])
        self.channels: list[int] = list(cfg.get("channels", []))
        self.offset = int(cfg.get("channel_offset", 0))

    async def listen(self, sink) -> None:
        backoff = 2
        url = f"{self.base}/cgi-bin/eventManager.cgi?action=attach&codes=[All]&heartbeat=5"
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=60)) as client:
            while True:
                try:
                    async with client.stream("GET", url, auth=self.auth) as resp:
                        if resp.status_code != 200:
                            raise httpx.HTTPError(f"HTTP {resp.status_code}")
                        sink.source_up(self.key, self.channels or None)
                        for ch in self.channels:
                            sink.set_online(ch + self.offset, True)
                        backoff = 2
                        log.info("Dahua oqimiga ulandi: %s", self.cfg["host"])
                        buf = ""
                        async for chunk in resp.aiter_text():
                            buf += chunk
                            events, buf = parse_stream(buf)
                            for ev in events:
                                if self.channels and ev["channel"] not in self.channels:
                                    continue
                                ev["channel"] += self.offset
                                sink.emit(ev)
                except (httpx.HTTPError, OSError) as e:
                    sink.source_down(self.key, [c + self.offset for c in self.channels] or None)
                    log.warning("Dahua aloqasi yo'q: %s (qayta urinish %s s)", e, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    async def snapshots(self, sink) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            while True:
                for ch in self.channels:
                    try:
                        r = await client.get(f"{self.base}/cgi-bin/snapshot.cgi?channel={ch}", auth=self.auth)
                        if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
                            sink.upload_snapshot(ch + self.offset, r.content)
                    except httpx.HTTPError:
                        pass
                await asyncio.sleep(1800)

    async def run(self, sink) -> None:
        await asyncio.gather(self.listen(sink), self.snapshots(sink))
