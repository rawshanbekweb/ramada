from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

from app import clock
from app.enums import Severity
from app.i18n import LANG_NAMES, money, t
from app.services.pricing import fmt_sum

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
env = templates.env
env.globals.update(t=t, LANG_NAMES=LANG_NAMES, now=clock.now)
env.filters["money"] = money
env.filters["sum"] = fmt_sum
env.filters["dt"] = lambda v: clock.fmt(v)
env.filters["time"] = lambda v: clock.fmt(v, with_date=False)
env.filters["d"] = lambda v: v.strftime("%d.%m.%Y") if v else "—"
env.filters["dm"] = lambda v: v.strftime("%d.%m") if v else "—"
env.filters["sev_class"] = lambda s: {Severity.CRITICAL: "crit", Severity.HIGH: "high", Severity.MEDIUM: "med"}.get(s, "")

STATUS_UZ = {"pending": "Kutilmoqda", "active": "Yashamoqda", "checked_out": "Chiqdi", "cancelled": "Bekor"}
ROOM_STATE_UZ = {"clean": "Toza", "dirty": "Iflos", "cleaning": "Tozalanmoqda", "out_of_order": "Ishlamaydi"}
ROLE_UZ = {"owner": "Ega", "admin": "Administrator", "reception": "Reception", "housekeeper": "Farrosh"}
env.globals.update(STATUS_UZ=STATUS_UZ, ROOM_STATE_UZ=ROOM_STATE_UZ, ROLE_UZ=ROLE_UZ)
