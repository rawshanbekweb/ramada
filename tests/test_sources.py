"""Kamera manbalari: Dahua, ONVIF, RTSP (sun'iy video), agent va server integratsiyasi."""
import base64
import json
import threading
import time

import numpy as np
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock
from app.main import app
from app.models import Alert, Camera, CameraZone
from app.security import edge_signature
from app.services import entry
from edge.agent import Agent, Config
from edge.sources.dahua import parse_stream
from edge.sources.onvif import parse_notifications, security_header
from tests.helpers import accept_and_pay_cash, new_booking, room


# ------------------------------------------------------------------ Dahua
DAHUA = (
    "--myboundary\r\nContent-Type: text/plain\r\nContent-Length: 40\r\n\r\nHeartbeat\r\n"
    "--myboundary\r\nContent-Type: text/plain\r\n\r\n"
    'Code=CrossRegionDetection;action=Start;index=1;data={\n   "Action" : "Appear",\n   "Name" : "Door 205",\n'
    '   "ObjectType" : "Human",\n   "Object" : { "BoundingBox" : [ 1, 2, 3, 4 ] }\n}\r\n'
    "--myboundary\r\n\r\nCode=VideoBlind;action=Start;index=0\r\n"
    "--myboundary\r\n\r\nCode=CrossLineDetection;action=Start;index=2;data={ \"Name\" : \"1\""
)


def test_dahua_parser_partial_chunks():
    events, rest = parse_stream(DAHUA)
    assert [(e["event_type"], e["channel"], e.get("region_id"), e.get("target"), e.get("state")) for e in events] == [
        ("enter", 2, 205, "human", None),
        ("tamper", 1, 0, None, "active"),
    ]
    assert rest.startswith("Code=CrossLineDetection")  # JSON hali kelmagan — buferda qoladi
    events2, _ = parse_stream(rest + " }\r\n")
    assert events2[0]["event_type"] == "line" and events2[0]["channel"] == 3


# ------------------------------------------------------------------ ONVIF
ONVIF = b"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
 xmlns:tt="http://www.onvif.org/ver10/schema" xmlns:tns1="http://www.onvif.org/ver10/topics"><s:Body><tev:PullMessagesResponse xmlns:tev="http://www.onvif.org/ver10/events/wsdl">
<wsnt:NotificationMessage><wsnt:Topic Dialect="x">tns1:RuleEngine/FieldDetector/ObjectsInside</wsnt:Topic><wsnt:Message>
 <tt:Message UtcTime="2026-12-15T10:01:00Z" PropertyOperation="Changed"><tt:Source><tt:SimpleItem Name="VideoSourceConfigurationToken" Value="vs0"/>
 <tt:SimpleItem Name="Rule" Value="Door3"/></tt:Source><tt:Data><tt:SimpleItem Name="IsInside" Value="true"/></tt:Data></tt:Message></wsnt:Message></wsnt:NotificationMessage>
<wsnt:NotificationMessage><wsnt:Topic Dialect="x">tns1:RuleEngine/TamperDetector/Tamper</wsnt:Topic><wsnt:Message>
 <tt:Message UtcTime="2026-12-15T10:02:00Z" PropertyOperation="Changed"><tt:Source/><tt:Data><tt:SimpleItem Name="IsTamper" Value="true"/></tt:Data></tt:Message></wsnt:Message></wsnt:NotificationMessage>
<wsnt:NotificationMessage><wsnt:Topic Dialect="x">tns1:VideoSource/MotionAlarm</wsnt:Topic><wsnt:Message>
 <tt:Message UtcTime="2026-12-15T10:00:00Z" PropertyOperation="Initialized"><tt:Source/><tt:Data><tt:SimpleItem Name="State" Value="true"/></tt:Data></tt:Message></wsnt:Message></wsnt:NotificationMessage>
</tev:PullMessagesResponse></s:Body></s:Envelope>"""


def test_onvif_notifications():
    evs = parse_notifications(ONVIF, channel=5)
    assert [(e["event_type"], e["region_id"], e.get("state")) for e in evs] == [("enter", 3, None), ("tamper", 0, "active")]
    assert evs[0]["occurred_at"] == "2026-12-15T10:01:00+00:00"


def test_onvif_password_digest():
    from datetime import datetime, timezone

    h = security_header("admin", "secret", datetime(2026, 1, 1, tzinfo=timezone.utc), b"0123456789abcdef")
    import hashlib

    expected = base64.b64encode(hashlib.sha1(b"0123456789abcdef" + b"2026-01-01T00:00:00Z" + b"secret").digest()).decode()
    assert expected in h and "<wsse:Username>admin</wsse:Username>" in h


# ------------------------------------------------------------------ Agent
def test_agent_emit_health_dedupe_and_channel_gaps(tmp_path):
    a = Agent(Config.from_dict({"server_url": "http://x", "edge_secret": "s", "queue_path": str(tmp_path / "q.db"),
                                "sources": [{"type": "rtsp", "channel": 1, "url": "x"}]}))
    a.source_up("rtsp:1", [1])  # birinchi ulanish — ishga tushish uzilishi yopiladi
    a.emit({"channel": 1, "region_id": 0, "event_type": "tamper", "state": "inactive", "occurred_at": "t"})
    a.emit({"channel": 1, "region_id": 0, "event_type": "tamper", "state": "active", "occurred_at": "t"})
    a.emit({"channel": 1, "region_id": 0, "event_type": "tamper", "state": "active", "occurred_at": "t"})
    a.emit({"channel": 1, "region_id": 2, "event_type": "dwell", "occurred_at": "t", "snapshot": b"\xff\xd8jpg"})
    q = a.queue.peek(10)
    assert [e["event_type"] for e in q] == ["tamper", "dwell"]
    assert base64.b64decode(q[1]["snapshot_b64"]) == b"\xff\xd8jpg" and "edge_received_at" in q[1]
    a.source_down("rtsp:1", [1])
    gaps = a.queue.gaps()
    assert gaps[-1]["channel"] == 1 and gaps[-1]["end"] is None
    assert not a.nvr_ok
    a.source_up("rtsp:1", [1])
    assert a.queue.gaps()[-1]["end"] is not None and a.nvr_ok


def test_legacy_config_converted():
    c = Config.from_dict({"server_url": "u", "edge_secret": "s", "nvr_host": "1.2.3.4", "nvr_user": "u", "nvr_password": "p"})
    assert c.sources[0]["type"] == "hikvision" and c.sources[0]["host"] == "1.2.3.4"


# ------------------------------------------------------------------ RTSP (sun'iy video)
def _make_video(path, seconds_before=2, stand=4, after=2, fps=10):
    import cv2

    H, W = 360, 640
    rng = np.random.default_rng(3)
    # koridor: yorug'lik gradienti, devor/eshik chegaralari va shovqin
    grad = np.linspace(60, 190, W)[None, :, None].repeat(H, 0).repeat(3, 2)
    base = (grad + rng.normal(0, 6, size=(H, W, 3))).clip(0, 255).astype(np.uint8)
    base[:, 300:310] = 30
    out = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), fps, (W, H))
    for i in range(int((seconds_before + stand + after) * fps)):
        f = base.copy()
        t = i / fps
        if seconds_before <= t < seconds_before + stand:
            f[100:330, 90:170] = 20  # odam eshik (chap zona) oldida turibdi
        out.write(f)
    out.release()


class FakeSink:
    def __init__(self, zones):
        self.events, self.snaps, self._zones, self.online = [], [], zones, []
        self.done = threading.Event()

    def emit(self, ev):
        self.events.append(ev)

    def set_online(self, ch, ok):
        self.online.append(ok)

    def source_up(self, key, chans):
        pass

    def source_down(self, key, chans):
        self.done.set()

    def zones_for(self, ch):
        return self._zones

    def upload_snapshot(self, ch, jpeg):
        self.snaps.append(jpeg)


def test_rtsp_source_detects_dwell_on_video_file(tmp_path):
    pytest.importorskip("cv2")
    from edge.sources.rtsp import RtspSource

    path = tmp_path / "corridor.avi"
    _make_video(path)
    zones = [{"region_id": 7, "kind": "door", "polygon": [[0.1, 0.25], [0.3, 0.25], [0.3, 0.95], [0.1, 0.95]]},
             {"region_id": 8, "kind": "door", "polygon": [[0.6, 0.25], [0.8, 0.25], [0.8, 0.95], [0.6, 0.95]]}]
    src = RtspSource({"channel": 3, "url": str(path), "fps": 5, "file_time": True, "file_start": time.time()})
    sink = FakeSink(zones)
    th = threading.Thread(target=src._loop, args=(sink,), daemon=True)
    th.start()
    sink.done.wait(20)
    src.stop.set()
    kinds = [(e["event_type"], e["region_id"]) for e in sink.events]
    assert ("enter", 7) in kinds and ("dwell", 7) in kinds
    assert not [k for k in kinds if k[1] == 8]
    assert sink.snaps and sink.snaps[0][:2] == b"\xff\xd8"
    assert any("snapshot" in e for e in sink.events if e["event_type"] == "dwell")


# ------------------------------------------------------------------ server
def _signed(c, path, payload):
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    return c.post(path, content=body, headers={"x-edge-timestamp": ts, "x-edge-signature": edge_signature("edge-test-secret", ts, body)})


def test_canonical_types_on_server(db, staff):
    weak = entry.ingest_event(db, channel=2, region_id=2, event_type="enter", occurred_at=clock.now())
    assert weak.strength == "weak" and weak.verdict == "R2?"
    strong = entry.ingest_event(db, channel=2, region_id=6, event_type="dwell", occurred_at=clock.now())
    assert strong.strength == "strong" and strong.verdict == "R2"
    line = entry.ingest_event(db, channel=10, region_id=1, event_type="line", occurred_at=clock.now())
    assert line.verdict == "entrance"
    t = entry.ingest_event(db, channel=1, region_id=0, event_type="tamper", state="active",
                           occurred_at=clock.now(), raw={"reason": "covered"})
    db.commit()
    assert "qorong'i" in db.scalar(select(Alert).where(Alert.zone_event_id == t.id)).message


def test_snapshot_config_and_zone_editor(db, staff):
    c = TestClient(app, base_url="https://hotel.test")
    jpeg = b"\xff\xd8" + b"0" * 100
    assert _signed(c, "/api/edge/snapshot", {"channel": 21, "jpeg_b64": base64.b64encode(jpeg).decode()}).status_code == 200
    cam = db.scalar(select(Camera).where(Camera.channel == 21))
    assert cam is not None and cam.snapshot_path
    c.post("/panel/login", data={"login": "owner", "password": "owner-pass-1"})
    page = c.get(f"/panel/cameras/{cam.id}/zones")
    assert page.status_code == 200 and "zone-wrap" in page.text
    import re

    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text).group(1)
    rid = room(db, "305").id
    r = c.post(f"/panel/cameras/{cam.id}/zones", data={"csrf": csrf, "kind": "door", "room_id": str(rid),
                                                      "polygon": json.dumps([[0.1, 0.1], [0.3, 0.1], [0.3, 0.5]])})
    assert r.status_code == 200
    db.expire_all()
    z = db.scalar(select(CameraZone).where(CameraZone.camera_id == cam.id))
    assert z.region_id == 1 and z.room_id == rid and len(z.polygon) == 3
    cfg = _signed(c, "/api/edge/config", {}).json()
    assert any(x["channel"] == 21 and x["polygon"] for x in cfg["zones"])
    assert c.get(f"/panel/cameras/{cam.id}/snapshot").content == jpeg
    # Edge zona ID'si bo'yicha hodisa → 305-xona
    ev = entry.ingest_event(db, channel=21, region_id=1, event_type="dwell", occurred_at=clock.now())
    assert ev.room_id == rid


def test_per_channel_stream_gap_only_affects_that_camera(db, staff):
    rec = staff["reception"]
    b1 = new_booking(db, rec, number="101", phone="+998901111111")
    accept_and_pay_cash(db, b1, rec)
    b2 = new_booking(db, rec, number="201", phone="+998902222222")
    accept_and_pay_cash(db, b2, rec)
    start = clock.now() + timedelta_min(2)
    clock.advance(minutes=31)
    entry.heartbeat(db, {"nvr_ok": True, "queue": 0, "gaps": [{"start": start.isoformat(), "end": None, "channel": 1}]})
    entry.sweep_windows(db)
    db.commit()
    r1 = db.scalars(select(Alert).where(Alert.rule == "R1")).all()
    assert [a.booking_id for a in r1] == [b2.id]


def timedelta_min(m):
    from datetime import timedelta

    return timedelta(minutes=m)


def test_hikvision_rtsp_url_helper():
    from edge.check import hikvision_url

    assert hikvision_url("192.168.1.64", "admin", "Ab@12#x", 1) == "rtsp://admin:Ab%4012%23x@192.168.1.64:554/Streaming/Channels/102"
    assert hikvision_url("192.168.1.100", "admin", "p", 7, sub=False).endswith("/Streaming/Channels/701")
    assert "@10.0.0.5:8554/" in hikvision_url("10.0.0.5:8554", "a", "b")
