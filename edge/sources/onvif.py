"""ONVIF (Profile S/T) — ko'pchilik arzon IP kameralar qo'llaydigan umumiy standart.

PullPoint usuli: CreatePullPointSubscription → PullMessages (uzun so'rov) → Renew.
Autentifikatsiya: WS-Security UsernameToken (PasswordDigest).

Eslatma: arzon kameralarning ko'pi ONVIF orqali faqat *butun kadr* bo'yicha harakat
(MotionAlarm) beradi — bu eshikni ajratmaydi. Eshik darajasida aniqlik kerak bo'lsa,
kamerada «hudud» qoidalari (FieldDetector) bo'lishi yoki `rtsp` manbasi
(mini-PC'dagi tahlil) ishlatilishi kerak.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import re
import uuid
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

import httpx

log = logging.getLogger("edge.onvif")

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "tt": "http://www.onvif.org/ver10/schema",
    "wsa": "http://www.w3.org/2005/08/addressing",
}
TEV = "http://www.onvif.org/ver10/events/wsdl"


def security_header(user: str, password: str, now: datetime | None = None, nonce: bytes | None = None) -> str:
    nonce = nonce or os.urandom(16)
    created = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return (
        '<wsse:Security s:mustUnderstand="1" '
        'xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
        'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f"<wsse:UsernameToken><wsse:Username>{user}</wsse:Username>"
        '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
        f"{digest}</wsse:Password>"
        '<wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f"{base64.b64encode(nonce).decode()}</wsse:Nonce><wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>"
    )


def envelope(user: str, password: str, body: str, action: str | None = None, to: str | None = None) -> str:
    addr = ""
    if action:
        addr += f'<wsa:Action>{action}</wsa:Action><wsa:MessageID>urn:uuid:{uuid.uuid4()}</wsa:MessageID>'
    if to:
        addr += f"<wsa:To>{to}</wsa:To>"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:wsa="http://www.w3.org/2005/08/addressing" '
        f'xmlns:tev="{TEV}" xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2">'
        f"<s:Header>{security_header(user, password)}{addr}</s:Header><s:Body>{body}</s:Body></s:Envelope>"
    )


def parse_subscription_address(xml: bytes) -> str | None:
    root = ET.fromstring(xml)
    el = root.find(".//wsnt:SubscriptionReference/wsa:Address", NS)
    return el.text.strip() if el is not None and el.text else None


def _items(el, tag: str) -> dict[str, str]:
    out = {}
    if el is None:
        return out
    for item in el.findall(f".//tt:{tag}/tt:SimpleItem", NS):
        out[item.get("Name", "")] = item.get("Value", "")
    return out


def _true(v: str | None) -> bool | None:
    if v is None:
        return None
    return v.strip().lower() in ("true", "1")


def _region(source: dict) -> int:
    for key in ("Rule", "RuleName", "Region", "Field"):
        if key in source:
            digits = re.sub(r"\D", "", source[key])
            if digits:
                return int(digits)
    return 0


def parse_notifications(xml: bytes, channel: int) -> list[dict]:
    """PullMessages javobidagi NotificationMessage'larni kanonik hodisalarga aylantiradi."""
    root = ET.fromstring(xml)
    events = []
    for nm in root.findall(".//wsnt:NotificationMessage", NS):
        topic_el = nm.find("wsnt:Topic", NS)
        topic = (topic_el.text or "").strip() if topic_el is not None else ""
        msg = nm.find(".//tt:Message", NS)
        if msg is None:
            continue
        utc = msg.get("UtcTime")
        source = _items(msg, "Source")
        data = _items(msg, "Data")
        if msg.get("PropertyOperation") == "Initialized":
            continue  # obuna boshidagi joriy holat — hodisa emas
        t = topic.split(":", 1)[-1]
        ev = None
        if t.endswith("FieldDetector/ObjectsInside") and _true(data.get("IsInside")):
            ev = {"event_type": "enter", "region_id": _region(source)}
        elif t.endswith("LineDetector/Crossed"):
            ev = {"event_type": "line", "region_id": _region(source)}
        elif t.endswith("CellMotionDetector/Motion") and _true(data.get("IsMotion")):
            ev = {"event_type": "motion", "region_id": _region(source)}
        elif t.endswith("VideoSource/MotionAlarm") and _true(data.get("State")):
            ev = {"event_type": "motion", "region_id": 0}
        elif t.endswith("TamperDetector/Tamper") or t.endswith("GlobalSceneChange/ImagingService"):
            v = _true(data.get("IsTamper", data.get("State")))
            if v is not None:
                ev = {"event_type": "tamper", "region_id": 0, "state": "active" if v else "inactive"}
        elif t.endswith("VideoSource/SignalLoss"):
            v = _true(data.get("State"))
            if v is not None:
                ev = {"event_type": "videoloss", "region_id": 0, "state": "active" if v else "inactive"}
        if ev is None:
            continue
        ev.update({"channel": channel, "vendor_type": t, "occurred_at": utc or datetime.now(timezone.utc).isoformat()})
        if ev["occurred_at"].endswith("Z"):
            ev["occurred_at"] = ev["occurred_at"][:-1] + "+00:00"
        events.append(ev)
    return events


class OnvifSource:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.channel = int(cfg["channel"])
        self.key = f"onvif:{cfg['host']}"
        self.url = cfg.get("events_url") or f"http://{cfg['host']}/onvif/event_service"
        self.user, self.password = cfg["user"], cfg["password"]

    async def _post(self, client, url, body, action=None, to=None) -> bytes:
        r = await client.post(
            url,
            content=envelope(self.user, self.password, body, action, to).encode(),
            headers={"Content-Type": "application/soap+xml; charset=utf-8"},
        )
        if r.status_code != 200:
            raise httpx.HTTPError(f"ONVIF HTTP {r.status_code}: {r.text[:200]}")
        return r.content

    async def run(self, sink) -> None:
        backoff = 2
        async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=40)) as client:
            while True:
                try:
                    resp = await self._post(
                        client, self.url,
                        "<tev:CreatePullPointSubscription><tev:InitialTerminationTime>PT600S</tev:InitialTerminationTime>"
                        "</tev:CreatePullPointSubscription>",
                        action=f"{TEV}/EventPortType/CreatePullPointSubscriptionRequest",
                    )
                    sub = parse_subscription_address(resp) or self.url
                    sink.source_up(self.key, [self.channel])
                    sink.set_online(self.channel, True)
                    backoff = 2
                    log.info("ONVIF obuna: %s", sub)
                    renewed = asyncio.get_event_loop().time()
                    while True:
                        xml = await self._post(
                            client, sub,
                            "<tev:PullMessages><tev:Timeout>PT20S</tev:Timeout><tev:MessageLimit>50</tev:MessageLimit></tev:PullMessages>",
                            action=f"{TEV}/PullPointSubscription/PullMessagesRequest", to=sub,
                        )
                        for ev in parse_notifications(xml, self.channel):
                            sink.emit(ev)
                        if asyncio.get_event_loop().time() - renewed > 300:
                            await self._post(
                                client, sub,
                                "<wsnt:Renew><wsnt:TerminationTime>PT600S</wsnt:TerminationTime></wsnt:Renew>",
                                action="http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest", to=sub,
                            )
                            renewed = asyncio.get_event_loop().time()
                except (httpx.HTTPError, OSError, ET.ParseError) as e:
                    sink.source_down(self.key, [self.channel])
                    sink.set_online(self.channel, False)
                    log.warning("ONVIF aloqasi yo'q (%s): %s", self.cfg["host"], e)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
