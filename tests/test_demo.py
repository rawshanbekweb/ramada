from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.demo import DemoError, generate
from app.main import app
from app.models import Alert, Booking
from app.services import reports


def test_demo_history(db, staff):
    st = generate(db, days=6)
    db.commit()
    assert st["bookings"] >= 40
    rules = {r for (r,) in db.execute(select(Alert.rule).distinct())}
    assert {"R3", "R7", "R9"} <= rules, rules
    full = db.scalar(select(func.count(Booking.id)).where(Booking.stage == 3))
    assert full / st["bookings"] > 0.6
    assert "Haftalik" in reports.weekly_text(db, raise_r11=False)
    c = TestClient(app, base_url="https://hotel.test")
    c.post("/panel/login", data={"login": "owner", "password": "owner-pass-1"})
    page = c.get("/panel/")
    assert page.status_code == 200 and "DEMO" in page.text
    try:
        generate(db, days=1)
        assert False
    except DemoError:
        pass
