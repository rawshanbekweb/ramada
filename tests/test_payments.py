import hashlib

from fastapi.testclient import TestClient

from app.main import app
from app.models import Booking
from app.services import bookings
from tests.helpers import new_booking, payme_auth


def _rpc(client, method, params, auth=None):
    return client.post("/api/payme", json={"id": 1, "method": method, "params": params},
                       headers={"Authorization": auth or payme_auth()}).json()


def test_payme_full_flow(db, staff, telegram):
    b = new_booking(db, staff["reception"])
    client = TestClient(app)
    amount = b.final_amount * 100
    acc = {"booking_id": str(b.id)}
    # Mehmon hali tasdiqlamagan
    r = _rpc(client, "CheckPerformTransaction", {"amount": amount, "account": acc})
    assert r["error"]["code"] <= -31050
    bookings.guest_accept(db, b, "dev")
    db.commit()
    assert _rpc(client, "CheckPerformTransaction", {"amount": amount - 100, "account": acc})["error"]["code"] == -31001
    assert _rpc(client, "CheckPerformTransaction", {"amount": amount, "account": acc})["result"]["allow"] is True
    assert _rpc(client, "CheckPerformTransaction", {"amount": amount, "account": acc}, auth="Basic eA==")["error"]["code"] == -32504
    r = _rpc(client, "CreateTransaction", {"id": "pm-1", "time": 1, "amount": amount, "account": acc})
    assert r["result"]["state"] == 1
    # Ikkinchi parallel tranzaksiya — rad
    r2 = _rpc(client, "CreateTransaction", {"id": "pm-2", "time": 2, "amount": amount, "account": acc})
    assert "error" in r2
    r = _rpc(client, "PerformTransaction", {"id": "pm-1"})
    assert r["result"]["state"] == 2
    # Idempotent
    assert _rpc(client, "PerformTransaction", {"id": "pm-1"})["result"]["state"] == 2
    db.expire_all()
    b = db.get(Booking, b.id)
    assert b.stage == 1 and b.paid_amount == b.final_amount
    assert _rpc(client, "CheckTransaction", {"id": "pm-1"})["result"]["state"] == 2
    st = _rpc(client, "GetStatement", {"from": 0, "to": 10})["result"]["transactions"]
    assert len(st) == 1
    # Qaytarish — R10
    r = _rpc(client, "CancelTransaction", {"id": "pm-1", "reason": 5})
    assert r["result"]["state"] == -2
    db.expire_all()
    assert db.get(Booking, b.id).paid_amount == 0


def _click_sign(*parts):
    return hashlib.md5("".join(str(p) for p in parts).encode()).hexdigest()


def test_click_flow(db, staff):
    b = new_booking(db, staff["reception"])
    bookings.guest_accept(db, b, "dev")
    db.commit()
    client = TestClient(app)
    base = {"click_trans_id": "c1", "service_id": "111", "click_paydoc_id": "9", "merchant_trans_id": str(b.id),
            "amount": f"{b.final_amount}.00", "sign_time": "2026-12-15 10:00:00", "error": "0"}
    prep = dict(base, action="0")
    prep["sign_string"] = "bad"
    assert client.post("/api/click/prepare", data=prep).json()["error"] == -1
    prep["sign_string"] = _click_sign("c1", "111", "click-secret", b.id, prep["amount"], 0, prep["sign_time"])
    r = client.post("/api/click/prepare", data=prep).json()
    assert r["error"] == 0
    mp = r["merchant_prepare_id"]
    comp = dict(base, action="1", merchant_prepare_id=str(mp))
    comp["sign_string"] = _click_sign("c1", "111", "click-secret", b.id, mp, comp["amount"], 1, comp["sign_time"])
    r = client.post("/api/click/complete", data=comp).json()
    assert r["error"] == 0
    assert client.post("/api/click/complete", data=comp).json()["error"] == -4
    db.expire_all()
    assert db.get(Booking, b.id).stage == 1
