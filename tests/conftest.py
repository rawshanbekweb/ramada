import os

TEST_DB = os.environ.get("SQ_TEST_DATABASE_URL", "sqlite://")
os.environ["SQ_DATABASE_URL"] = TEST_DB
os.environ["SQ_SECRET_KEY"] = "test-secret-key-0123456789abcdef"
os.environ["SQ_EDGE_SECRET"] = "edge-test-secret"
os.environ["SQ_PAYME_MERCHANT_ID"] = "test-merchant"
os.environ["SQ_PAYME_KEY"] = "payme-test-key"
os.environ["SQ_CLICK_SERVICE_ID"] = "111"
os.environ["SQ_CLICK_MERCHANT_ID"] = "222"
os.environ["SQ_CLICK_SECRET_KEY"] = "click-secret"
os.environ["SQ_BASE_URL"] = "https://hotel.test"
os.environ["SQ_STORAGE_DIR"] = "/tmp/sq-test-storage"

from datetime import datetime  # noqa: E402

import pytest  # noqa: E402

from app import clock  # noqa: E402
from app.db import Base, SessionLocal, init_engine  # noqa: E402
from app.enums import Role  # noqa: E402
from app.integrations import sms as sms_mod  # noqa: E402
from app.integrations import telegram as tg  # noqa: E402
from app.models import User  # noqa: E402
from app.security import hash_password  # noqa: E402

engine = init_engine(TEST_DB)


@pytest.fixture(autouse=True)
def fresh_db():
    from app import models  # noqa: F401

    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    from app.cli import cmd_seed_demo

    cmd_seed_demo(None)
    clock.set_now(datetime(2026, 12, 15, 15, 0))  # seshanba, Toshkent vaqti
    yield
    clock.set_now(None)


@pytest.fixture
def telegram():
    fake = tg.FakeTelegram()
    tg.set_backend(fake)
    yield fake
    tg.set_backend(None)


@pytest.fixture
def sms():
    fake = sms_mod.ConsoleSms()
    sms_mod.set_provider(fake)
    yield fake
    sms_mod.set_provider(None)


@pytest.fixture
def db(telegram, sms):
    s = SessionLocal()
    yield s
    s.close()


@pytest.fixture
def staff(db):
    users = {
        "owner": User(full_name="Ega Egayev", role=Role.OWNER, telegram_id=1001, login="owner",
                      password_hash=hash_password("owner-pass-1")),
        "admin": User(full_name="Admin Adminov", role=Role.ADMIN, telegram_id=1002, login="admin",
                      password_hash=hash_password("admin-pass-1")),
        "reception": User(full_name="Resepshn Aliyev", role=Role.RECEPTION, telegram_id=1003),
        "reception2": User(full_name="Resepshn Valiyev", role=Role.RECEPTION, telegram_id=1005),
        "housekeeper": User(full_name="Farrosh Karimova", role=Role.HOUSEKEEPER, telegram_id=1004),
    }
    db.add_all(users.values())
    db.commit()
    return users
