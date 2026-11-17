"""Tashqi tizimlar: mehmonxonadagi edge agent (NVR hodisalari)."""
from __future__ import annotations

import base64
import binascii
import json
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models import Camera, CameraZone
from app.security import verify_edge_signature
from app.services import entry

router = APIRouter(prefix="/api")


@router.get("/health")
def health():
    return {"ok": True}


# ------------------------------------------------------------------ edge agent
async def _verified_json(request: Request) -> dict:
    body = await request.body()
    if not verify_edge_signature(request.headers.get("x-edge-timestamp"), request.headers.get("x-edge-signature"), body):
        raise HTTPException(401, "bad signature")
    try:
        return json.loads(body or b"{}")
    except json.JSONDecodeError:
        raise HTTPException(400, "bad json")


def _save_snapshot(b64: str | None, limit: int = 2_000_000) -> str | None:
    if not b64:
        return None
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    if len(data) > limit or not data.startswith(b"\xff\xd8"):
        return None
    folder = Path(get_settings().storage_dir) / "snapshots" / datetime.now().strftime("%Y%m%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}.jpg"
    path.write_bytes(data)
    return str(path)


@router.post("/edge/events")
async def edge_events(request: Request, db: Session = Depends(get_db)):
    """Edge agent NVR hodisalarini paket bilan yuboradi (internet uzilsa — keyin, navbatdan)."""
    payload = await _verified_json(request)
    accepted, duplicates = [], 0
    for item in payload.get("events", []):
        try:
            occurred = datetime.fromisoformat(item["occurred_at"])
            ev = entry.ingest_event(
                db,
                channel=int(item["channel"]),
                region_id=int(item.get("region_id") or 0),
                event_type=str(item.get("event_type", "regionEntrance")),
                occurred_at=occurred,
                edge_event_id=str(item.get("id")) if item.get("id") else None,
                snapshot_path=_save_snapshot(item.get("snapshot_b64")),
                raw={k: v for k, v in item.items() if k != "snapshot_b64"},
                target=item.get("target"),
                state=item.get("state"),
                edge_received_at=datetime.fromisoformat(item["edge_received_at"]) if item.get("edge_received_at") else None,
            )
        except (KeyError, ValueError, TypeError):
            continue
        if ev is None:
            duplicates += 1
        else:
            accepted.append(item.get("id"))
    # Paket bilan faqat navbat qoldig'i keladi; NVR holati va uzilishlar — heartbeat orqali
    entry.heartbeat(db, {"queue": int(payload.get("queue", 0) or 0)})
    return {"accepted": accepted, "duplicates": duplicates}


@router.post("/edge/heartbeat")
async def edge_heartbeat(request: Request, db: Session = Depends(get_db)):
    payload = await _verified_json(request)
    hb = {"nvr_ok": bool(payload.get("nvr_ok", True)), "queue": int(payload.get("queue", 0)), "version": payload.get("version")}
    if isinstance(payload.get("channels"), dict):
        hb["channels"] = payload["channels"]
    if isinstance(payload.get("gaps"), list):
        hb["gaps"] = payload["gaps"]
    entry.heartbeat(db, hb)
    return {"ok": True}


@router.post("/edge/snapshot")
async def edge_snapshot(request: Request, db: Session = Depends(get_db)):
    """Kamera kadri: panelda eshik zonalarini chizish uchun."""
    payload = await _verified_json(request)
    try:
        channel = int(payload["channel"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "channel")
    cam = db.scalar(select(Camera).where(Camera.channel == channel))
    if cam is None:
        cam = Camera(name=f"Kanal {channel}", channel=channel)
        db.add(cam)
        db.flush()
    path = _save_snapshot(payload.get("jpeg_b64"), limit=3_000_000)
    if path is None:
        raise HTTPException(400, "jpeg")
    cam.snapshot_path = path
    from app import clock

    cam.snapshot_at = clock.now()
    return {"ok": True}


@router.post("/edge/config")
async def edge_config(request: Request, db: Session = Depends(get_db)):
    """Edge agentga qaysi kanallar/hududlar kuzatilishini aytadi."""
    await _verified_json(request)
    cams = db.scalars(select(Camera)).all()
    zones = db.scalars(select(CameraZone)).all()
    return {
        "channels": sorted({c.channel for c in cams}),
        "zones": [
            {
                "channel": z.camera.channel,
                "region_id": z.region_id,
                "kind": z.kind,
                "room": z.room.number if z.room else None,
                "polygon": z.polygon,
            }
            for z in zones
        ],
    }
