"""Edge agent: mehmonxona ichidagi kichik kompyuterda (mini-PC) ishlaydi.

Kameralar brendga bog'liq emas. Har bir kamera yoki NVR `[[sources]]` bo'limida
quyidagi turlardan biri bilan ulanadi (edge/sources/):

    rtsp      — istalgan arzon IP kamera: video tahlil shu mini-PC'da (tavsiya etiladi)
    hikvision — Hikvision NVR/kamera ISAPI hodisalari
    dahua     — Dahua va uning OEM brendlari, CGI hodisalari
    onvif     — ONVIF qo'llaydigan har qanday kamera (PullPoint)

Agent vazifalari:
  * hodisalarni yagona ko'rinishga keltirib, lokal SQLite navbatiga yozadi va
    serverga HMAC imzo bilan paketlab yuboradi (internet uzilsa — keyin);
  * har bir hodisaga edge (NTP) vaqtini qo'shadi;
  * kamera sog'lig'i: kanal onlayn/oflayn, yopilish/videoloss (faqat holat o'zgarganda);
  * kuzatuv uzilishlarini (manba uzilgan yoki agentning o'zi ishlamagan davr) qayd etadi;
  * serverdan eshik zonalarini (rtsp uchun ko'pburchaklar) oladi, panel uchun
    kamera kadrlarini yuboradi;
  * har daqiqada heartbeat (FT-36).

Kameralar va NVR internetga ochilmaydi (TT 14-bo'lim).

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
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

from edge.queue import Queue
from edge.sources import build_source
from edge.sources.hikvision import extract_alerts, parse_alert, parse_channel_status  # noqa: F401 (qayta eksport)

log = logging.getLogger("edge")
VERSION = "2.0"
HEALTH_TYPES = {"tamper", "videoloss"}


@dataclass
class Config:
    server_url: str
    edge_secret: str
    sources: list[dict] = field(default_factory=list)
    queue_path: str = "edge/edge.queue.db"
    batch_size: int = 50
    heartbeat_seconds: int = 60
    config_refresh_seconds: int = 300
    verify_tls: bool = True

    @classmethod
    def load(cls, path: str) -> "Config":
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        data = dict(data)
        # Eski (1.x) format: bitta Hikvision NVR
        if "nvr_host" in data:
            data.setdefault("sources", []).append(
                {
                    "type": "hikvision",
                    "host": data.pop("nvr_host"),
                    "user": data.pop("nvr_user"),
                    "password": data.pop("nvr_password"),
                    "scheme": data.pop("nvr_scheme", "http"),
                    "channels": data.pop("channels", []),
                    "snapshots": data.pop("snapshots", True),
                }
            )
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sign(secret: str, body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"x-edge-timestamp": ts, "x-edge-signature": sig, "content-type": "application/json"}


class Agent:
    """Manbalar uchun umumiy «sink»: hodisa navbati, holatlar, uzilishlar, zonalar."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.queue = Queue(cfg.queue_path)
        self.channels: dict[str, bool] = {}
        self.health_state: dict[tuple[int, str], str] = {}
        self.down_sources: set[str] = set()
        self.zones: dict[int, list[dict]] = {}
        self.pending_snapshots: dict[int, bytes] = {}
        self.server = httpx.AsyncClient(base_url=cfg.server_url.rstrip("/"), timeout=20, verify=cfg.verify_tls)
        self.sources = [build_source(s) for s in cfg.sources]
        # Agent o'chib turgan davr — barcha kanallar uchun kuzatuv uzilishi
        last_alive = self.queue.meta_get("last_alive")
        self.queue.gap_open(last_alive or now_iso())
        self._startup_gap_open = True

    # ---------------------------------------------------------------- Sink
    @property
    def nvr_ok(self) -> bool:
        return not self.down_sources

    def emit(self, ev: dict) -> None:
        ev = dict(ev)
        if ev.get("event_type") in HEALTH_TYPES:
            key = (ev["channel"], ev["event_type"])
            if self.health_state.get(key, "inactive") == ev.get("state"):
                return  # holat o'zgarmagan (masalan, NVR'ning davriy «inactive» xabari)
            self.health_state[key] = ev.get("state")
        snap = ev.pop("snapshot", None)
        if snap:
            ev["snapshot_b64"] = base64.b64encode(snap).decode()
        ev["edge_received_at"] = now_iso()
        ev["id"] = f"{ev['channel']}-{ev.get('region_id', 0)}-{ev['occurred_at']}-{uuid.uuid4().hex[:6]}"
        self.queue.put(ev)
        log.info("Hodisa: kanal %s zona %s %s", ev["channel"], ev.get("region_id"), ev["event_type"])

    def set_online(self, channel: int, online: bool) -> None:
        self.channels[str(channel)] = bool(online)

    def source_down(self, key: str, channels: list[int] | None) -> None:
        if key in self.down_sources:
            return
        self.down_sources.add(key)
        if channels:
            for ch in channels:
                self.queue.gap_open(now_iso(), ch)
        else:
            self.queue.gap_open(now_iso())

    def source_up(self, key: str, channels: list[int] | None) -> None:
        if self._startup_gap_open:
            self.queue.gap_close(now_iso())
            self._startup_gap_open = False
        if key not in self.down_sources:
            return
        self.down_sources.discard(key)
        if channels:
            for ch in channels:
                self.queue.gap_close(now_iso(), ch)
        else:
            self.queue.gap_close(now_iso())

    def zones_for(self, channel: int) -> list[dict]:
        return self.zones.get(channel, [])

    def upload_snapshot(self, channel: int, jpeg: bytes) -> None:
        self.pending_snapshots[channel] = jpeg

    # ---------------------------------------------------------------- server
    async def _post(self, path: str, payload: dict) -> httpx.Response:
        body = json.dumps(payload).encode()
        return await self.server.post(path, content=body, headers=sign(self.cfg.edge_secret, body))

    async def push(self) -> None:
        while True:
            batch = self.queue.peek(self.cfg.batch_size)
            if batch:
                remaining = max(0, self.queue.size() - len(batch))
                try:
                    r = await self._post("/api/edge/events", {"events": batch, "nvr_ok": self.nvr_ok, "queue": remaining})
                    if r.status_code == 200:
                        self.queue.ack([e["id"] for e in batch])
                        continue
                    log.error("Server rad etdi: %s %s", r.status_code, r.text[:200])
                except httpx.HTTPError as e:
                    log.warning("Serverga ulanib bo'lmadi: %s (navbat: %s)", e, self.queue.size())
            for ch, jpeg in list(self.pending_snapshots.items()):
                try:
                    r = await self._post("/api/edge/snapshot", {"channel": ch, "jpeg_b64": base64.b64encode(jpeg).decode()})
                    if r.status_code == 200:
                        self.pending_snapshots.pop(ch, None)
                except httpx.HTTPError:
                    break
            await asyncio.sleep(2)

    async def heartbeat(self) -> None:
        while True:
            if self.nvr_ok:
                self.queue.meta_set("last_alive", now_iso())
            try:
                await self._post(
                    "/api/edge/heartbeat",
                    {
                        "nvr_ok": self.nvr_ok,
                        "queue": self.queue.size(),
                        "version": VERSION,
                        "channels": self.channels,
                        "gaps": self.queue.gaps(),
                    },
                )
            except httpx.HTTPError as e:
                log.warning("Heartbeat yuborilmadi: %s", e)
            await asyncio.sleep(self.cfg.heartbeat_seconds)

    async def refresh_config(self) -> None:
        """Serverdan eshik zonalari (rtsp manbalari uchun ko'pburchaklar) — har 5 daqiqada."""
        while True:
            try:
                r = await self._post("/api/edge/config", {})
                if r.status_code == 200:
                    zones: dict[int, list[dict]] = {}
                    for z in r.json().get("zones", []):
                        if z.get("polygon"):
                            zones.setdefault(int(z["channel"]), []).append(
                                {
                                    "region_id": z["region_id"],
                                    "kind": "line" if z["kind"] == "entrance" else "door",
                                    "polygon": z["polygon"],
                                }
                            )
                    self.zones = zones
            except (httpx.HTTPError, ValueError) as e:
                log.warning("Konfiguratsiya olinmadi: %s", e)
            await asyncio.sleep(self.cfg.config_refresh_seconds)

    async def run(self) -> None:
        if not self.sources:
            raise SystemExit("config.toml da [[sources]] yo'q")
        await asyncio.gather(
            self.push(), self.heartbeat(), self.refresh_config(), *(s.run(self) for s in self.sources)
        )


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--config", default="edge/config.toml")
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(Agent(Config.load(args.config)).run())


if __name__ == "__main__":
    main()
