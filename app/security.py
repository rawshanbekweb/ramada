"""Tokenlar, imzolar va parollar."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from app.config import get_settings


def _key(purpose: str) -> bytes:
    return hashlib.sha256(f"{purpose}:{get_settings().secret_key}".encode()).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# --- Xona QR tokeni -------------------------------------------------------
# Token = <room_id>.<version>.<imzo>. Imzo HMAC-SHA256 dan olinadi, shuning
# uchun boshqa xona yoki eski versiya uchun kodni soxtalashtirib bo'lmaydi.

def make_room_token(room_id: int, version: int) -> str:
    body = f"{room_id}.{version}"
    sig = hmac.new(_key("room-qr"), body.encode(), hashlib.sha256).digest()[:12]
    return f"{body}.{_b64(sig)}"


def parse_room_token(token: str) -> tuple[int, int] | None:
    try:
        room_s, ver_s, sig = token.split(".")
        room_id, version = int(room_s), int(ver_s)
    except (ValueError, AttributeError):
        return None
    expected = make_room_token(room_id, version).rsplit(".", 1)[1]
    if not hmac.compare_digest(expected, sig):
        return None
    return room_id, version


# --- Mehmon havolasi ------------------------------------------------------

def new_guest_token() -> str:
    return secrets.token_urlsafe(18)


def new_device_id() -> str:
    return secrets.token_urlsafe(24)


def device_hash(device_id: str | None) -> str | None:
    if not device_id:
        return None
    return hmac.new(_key("device"), device_id.encode(), hashlib.sha256).hexdigest()


def new_invite_code() -> str:
    return secrets.token_hex(4).upper()


# --- Parollar (stdlib scrypt) -----------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, salt_s, dk_s = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.scrypt(password.encode(), salt=_unb64(salt_s), n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(dk, _unb64(dk_s))


# --- Imzolangan cookie (panel sessiyasi) -----------------------------------

def sign_payload(data: dict, purpose: str, ttl_seconds: int) -> str:
    payload = dict(data, exp=int(time.time()) + ttl_seconds)
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64(hmac.new(_key(purpose), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def unsign_payload(token: str | None, purpose: str) -> dict | None:
    if not token or "." not in token:
        return None
    body, sig = token.rsplit(".", 1)
    expected = _b64(hmac.new(_key(purpose), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        data = json.loads(_unb64(body))
    except (ValueError, json.JSONDecodeError):
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data


# --- Edge agent so'rovlari imzosi -----------------------------------------

EDGE_MAX_SKEW = 300  # soniya


def edge_signature(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def verify_edge_signature(timestamp: str | None, signature: str | None, body: bytes) -> bool:
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(time.time() - ts) > EDGE_MAX_SKEW:
        return False
    expected = edge_signature(get_settings().edge_secret, timestamp, body)
    return hmac.compare_digest(expected, signature)
