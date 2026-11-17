"""Kamera manbalari. Har biri hodisalarni yagona (kanonik) ko'rinishda agentga beradi.

Kanonik hodisa turlari (serverga shu nomlar bilan boradi):
    dwell     — odam eshik oldida turdi (kuchli dalil)
    enter     — eshik zonasiga kirish / qisqa harakat (kuchsiz dalil)
    motion    — umumiy harakat (kuchsiz dalil)
    line      — asosiy kirishdagi chiziqni kesish (sanoq)
    tamper    — kamera yopildi / burildi (state: active/inactive)
    videoloss — videosignal yo'q (state: active/inactive)

Manbalar:
    rtsp      — istalgan arzon kamera: tahlil mini-PC'da (edge/motion.py)
    hikvision — Hikvision NVR/kamera ISAPI hodisalari
    dahua     — Dahua NVR/kamera (va ko'plab OEM: Imou, IMOU, Amcrest...) CGI hodisalari
    onvif     — ONVIF Profile S/T qo'llaydigan har qanday kamera (PullPoint)
"""
from __future__ import annotations

from typing import Protocol


class Sink(Protocol):
    """Agent tomonidan manbalarga beriladigan interfeys."""

    def emit(self, event: dict) -> None: ...

    def set_online(self, channel: int, online: bool) -> None: ...

    def source_down(self, key: str, channels: list[int] | None) -> None: ...

    def source_up(self, key: str, channels: list[int] | None) -> None: ...

    def zones_for(self, channel: int) -> list[dict]: ...

    def upload_snapshot(self, channel: int, jpeg: bytes) -> None: ...


def build_source(cfg: dict):
    kind = cfg.get("type", "rtsp")
    if kind == "rtsp":
        from edge.sources.rtsp import RtspSource

        return RtspSource(cfg)
    if kind == "hikvision":
        from edge.sources.hikvision import HikvisionSource

        return HikvisionSource(cfg)
    if kind == "dahua":
        from edge.sources.dahua import DahuaSource

        return DahuaSource(cfg)
    if kind == "onvif":
        from edge.sources.onvif import OnvifSource

        return OnvifSource(cfg)
    raise ValueError(f"Noma'lum manba turi: {kind}")
