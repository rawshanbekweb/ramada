# Shaffof qabul

Mehmonxonaga har bir joylashtirishni **uch mustaqil bosqichda** tasdiqlaydigan tizim (Nukus, TT v3.0):

1. **Pul tasdig'i**: mehmon SMS'dagi havola orqali rasmiy narxni ko'radi, tasdiqlaydi va to'laydi (Payme/Click yoki fiskal chekli naqd/terminal).
2. **Kirish tasdig'i**: to'lovdan keyin ochiladigan kirish oynasida koridor kamerasi (Hikvision NVR, ISAPI) shu xona eshigiga kelinganini qayd etadi.
3. **Xonada tasdiq**: mehmon xonadagi imzolangan QR plitani skanerlab, «shu xonadaman, shu summani to'ladim» deydi va Wi-Fi parolini oladi.

Biror bosqich o'tmasa yoki bronsiz xonada faollik bo'lsa, ega Telegram orqali darhol xabar oladi (R1–R12 qoidalari).

## Tarkib

| Qism | Fayllar | Kim ishlatadi |
|---|---|---|
| Server (FastAPI) | `app/main.py`, `app/web/` | — |
| Mehmon sahifasi (4 til) | `app/web/guest.py`, `templates/guest/` | Mehmon, SMS havolasi va xona QR'i orqali |
| Ega veb-paneli | `app/web/panel.py`, `templates/panel/` | Ega, administrator |
| Telegram bot | `app/bot/` | Reception, administrator, ega, farrosh |
| Biznes-mantiq | `app/services/` | — |
| Payme / Click / SMS / Telegram | `app/integrations/` | — |
| Rejalashtiruvchi (qoidalar, hisobotlar) | `app/worker.py`, `app/services/rules.py` | — |
| Edge agent (NVR yonida) | `edge/agent.py` | Mehmonxonadagi mini-PC |
| Migratsiyalar | `migrations/` | — |
| Testlar (TT 16-bo'lim, 12 ta sinov va boshqalar) | `tests/` | — |

TT talablari kodning qaysi joyida bajarilgani [docs/TT_MUVOFIQLIK.md](docs/TT_MUVOFIQLIK.md) faylida ko'rsatilgan.

## Tez ishga tushirish (ishlab chiqish)

```bash
uv venv .venv && . .venv/bin/activate
uv pip install -e ".[dev]"
python -m app.cli init --owner "Ega Ismi" --login owner --password 'kamida8belgi'
python -m app.cli seed-demo        # 40 xona, 3 toifa, namuna tariflar va kameralar
uvicorn app.main:app --reload      # http://localhost:8000/panel/
python -m app.worker               # bot + rejalashtiruvchi (SQ_TELEGRAM_BOT_TOKEN kerak)
pytest                             # 33 ta test
```

Sukut bo'yicha SQLite ishlatiladi. PostgreSQL bilan ishlatish uchun:

```bash
export SQ_DATABASE_URL=postgresql+psycopg://user:pass@localhost/shaffof
alembic upgrade head
SQ_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost/shaffof_test pytest
```

## Ishlab chiqarish

[docs/DEPLOY.md](docs/DEPLOY.md) faylida to'liq tavsiflangan. Qisqacha:

```bash
cp .env.example .env   # qiymatlarni to'ldiring
docker compose up -d --build
docker compose exec web python -m app.cli init --owner "..." --login owner
```

Server O'zbekiston hududida joylashgan bo'lishi shart («Shaxsga doir ma'lumotlar to'g'risida»gi Qonun).

## Asosiy sozlamalar

- **Muhit** (`.env`, `SQ_` prefiksi bilan): baza, domen, Telegram tokeni, Payme/Click kalitlari, SMS, edge kaliti. Ro'yxat `.env.example` faylida.
- **Biznes sozlamalari** bazada saqlanadi va panelda o'zgartiriladi: kirish oynasi (10–120 daqiqa), chegirma chegarasi, Wi-Fi paroli, kirish/chiqish soatlari, erta/kech haqlari, hisobot soati va boshqalar.
- **Tariflar** (toifa × kun turi × mavsum) faqat ega tomonidan panelda o'zgartiriladi; har bir o'zgarish jurnalga yoziladi.

## Webhook manzillari

| Tizim | URL |
|---|---|
| Payme Merchant API | `POST https://DOMEN/api/payme` |
| Click Prepare / Complete | `POST https://DOMEN/api/click/prepare`, `/api/click/complete` |
| Edge agent | `POST https://DOMEN/api/edge/events`, `/api/edge/heartbeat` (HMAC imzo bilan) |
