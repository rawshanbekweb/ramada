"""Telegram bot oqimlari: soxta Bot sessiyasi bilan yangilanishlarni berib tekshiramiz."""
import asyncio
from datetime import datetime

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User as TgUser
from sqlalchemy import select

from app.bot.main import build_dispatcher
from app.models import Booking, User


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method: TelegramMethod, timeout=None):
        self.calls.append(method)
        if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup)):
            return True
        chat_id = getattr(method, "chat_id", 1) or 1
        text = getattr(method, "text", None) or getattr(method, "caption", None)
        if isinstance(method, EditMessageText):
            pass
        return Message(message_id=len(self.calls), date=datetime.now(), chat=Chat(id=chat_id, type="private"), text=text)

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        raise RuntimeError("no download in tests")
        yield b""

    def texts(self):
        return [getattr(c, "text", "") or "" for c in self.calls]


_DP = None


def dispatcher():
    global _DP
    if _DP is None:
        _DP = build_dispatcher()
    return _DP


class Harness:
    def __init__(self, tg_id):
        self.session = FakeSession()
        self.bot = Bot("123:abc", session=self.session)
        self.dp = dispatcher()
        self.tg_id = tg_id
        self.n = 0

    def _user(self):
        return TgUser(id=self.tg_id, is_bot=False, first_name="X")

    def send(self, text=None, photo=False, contact=None):
        self.n += 1
        kw = {}
        if photo:
            kw["photo"] = [PhotoSize(file_id="ph1", file_unique_id="u1", width=10, height=10)]
        msg = Message(message_id=self.n, date=datetime.now(), chat=Chat(id=self.tg_id, type="private"),
                      from_user=self._user(), text=text, **kw)
        asyncio.run(self.dp.feed_update(self.bot, Update(update_id=self.n, message=msg)))

    def click(self, data):
        self.n += 1
        msg = Message(message_id=self.n, date=datetime.now(), chat=Chat(id=self.tg_id, type="private"), text="x")
        cb = CallbackQuery(id=str(self.n), from_user=self._user(), chat_instance="c", data=data, message=msg)
        asyncio.run(self.dp.feed_update(self.bot, Update(update_id=self.n, callback_query=cb)))

    def last_buttons(self):
        for c in reversed(self.session.calls):
            rm = getattr(c, "reply_markup", None)
            if rm is not None and hasattr(rm, "inline_keyboard"):
                return [b.callback_data for row in rm.inline_keyboard for b in row]
        return []


def test_invite_and_new_guest_flow(db, staff):
    u = User(full_name="Yangi Resepshn", role="reception", invite_code="ABCD1234", lang="uz")
    db.add(u)
    db.commit()
    h = Harness(5555)
    h.send("/start ABCD1234")
    assert any("Xush kelibsiz" in x for x in h.session.texts())
    h.send("💵 Smena")
    h.send("100000")
    assert any("Smena ochildi" in x for x in h.session.texts())
    h.send("🆕 Yangi mehmon")
    room_cb = [b for b in h.last_buttons() if b.startswith("ng:room:")][0]
    h.click(room_cb)
    h.click("ng:n:2")
    h.send("Aliyev Vali")
    h.send("AB1234567")
    h.send(photo=True)
    h.send("+998 90 765 43 21")
    h.click("ng:sp:1")
    h.click("ng:here:1")
    h.click("ng:dq:0")
    assert any("Davom etamizmi" in x for x in h.session.texts())
    h.click("ng:ok")
    db.expire_all()
    b = db.scalar(select(Booking).where(Booking.created_by_id == u.id))
    assert b is not None and b.guest.phone == "+998907654321" and b.nights == 2
    # Kalit — tasdiqsiz rad
    h.click(f"ba:key:{b.id}")
    assert any("Kalit berilmaydi" in x for x in h.session.texts())
    # Naqd to'lov
    h.click(f"ba:cash:{b.id}")
    h.send(str(b.final_amount))
    h.send("FISK-77")
    db.expire_all()
    b = db.get(Booking, b.id)
    assert b.paid_amount == b.final_amount and b.payments[0].receipt_no == "FISK-77"
    # Smenani yopish (ko'r hisob)
    h.click("sh:close")
    h.send(str(100000 + b.final_amount))
    assert any("Smena yopildi" in x for x in h.session.texts())


def test_housekeeper_morning_and_owner_alert_ack(db, staff):
    h = Harness(1004)  # farrosh
    h.send("🧹 Ertalabki tekshiruv")
    first = [b for b in h.last_buttons() if b.startswith("hm:") and b != "hm:save"][0]
    h.click(first)
    h.click("hm:save")
    from app.models import Alert

    db.expire_all()
    a = db.scalar(select(Alert).where(Alert.rule == "R7"))
    assert a is not None
    o = Harness(1001)  # ega
    o.click(f"al:ack:{a.id}")
    db.expire_all()
    assert db.get(Alert, a.id).status == "ack"
    o.send("📊 Bugun")
    assert any("Hozir" in x for x in o.session.texts())


def test_menu_button_resets_unfinished_flow(db, staff):
    h = Harness(1003)  # reception
    h.send("💵 Smena")
    h.send("0")
    h.send("🆕 Yangi mehmon")
    room_cb = [b for b in h.last_buttons() if b.startswith("ng:room:")][0]
    h.click(room_cb)  # endi «Necha tun?» kutilmoqda
    h.send("📋 Mehmonlar")  # jarayon tashlab ketildi
    before = len(h.session.calls)
    h.send("3")  # eski jarayonga «3 tun» bo'lib yozilmasligi kerak
    texts = [getattr(c, "text", "") or "" for c in h.session.calls[before:]]
    assert not any("F.I.Sh" in t for t in texts)
