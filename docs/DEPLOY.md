# O'rnatish

## 1. Server (O'zbekistondagi VPS, 2 vCPU / 2–4 GB RAM yetarli)

```bash
git clone <repo> shaffof && cd shaffof
cp .env.example .env && nano .env      # DOMAIN, POSTGRES_PASSWORD, SQ_* qiymatlari
docker compose up -d --build
docker compose exec web python -m app.cli init --owner "Ega Ismi" --login owner
docker compose exec web python -m app.cli seed-demo   # ixtiyoriy: 40 xonalik boshlang'ich tuzilma
```

Caddy HTTPS sertifikatini avtomatik oladi. Zaxira nusxa har kuni 03:30 da `backups` hajmiga yoziladi va 90 kun saqlanadi.

## 2. Telegram

1. @BotFather'da bot yarating va tokenni `SQ_TELEGRAM_BOT_TOKEN` ga yozing.
2. Panel → Xodimlar bo'limida xodimni qo'shing. Xodim botga `/start KOD` yuboradi.
3. Ega ham botga o'z kodini yuboradi (kod `init` buyrug'i natijasida chiqadi).

## 3. Payme

Biznes kabinetda kassa yarating. Endpoint: `https://DOMEN/api/payme`. Hisob maydoni: `booking_id`.
Kassa ID → `SQ_PAYME_MERCHANT_ID`, kalit → `SQ_PAYME_KEY`. Sinov uchun test kalitni va `SQ_PAYME_CHECKOUT_URL=https://checkout.test.paycom.uz` ni ishlating.

## 4. Click

Prepare URL: `https://DOMEN/api/click/prepare`, Complete URL: `https://DOMEN/api/click/complete`.
`service_id`, `merchant_id`, `secret_key` qiymatlari `.env` ga yoziladi.

## 5. SMS (eskiz.uz)

`SQ_SMS_PROVIDER=eskiz`, login va parol kiritiladi. SMS shablonlarini eskiz'da oldindan tasdiqlatib olish kerak (matnlar `app/i18n/messages.py` faylida, `sms.*` kalitlari ostida).

## 6. Kameralar va edge agent

1. NVR'da har bir koridor kamerasida xona eshigi oldiga **hududga kirish** (Region Entrance) hududini chizing. Asosiy kirishga **chiziq kesish** (Line Crossing) qo'shing. Hodisalar uchun «Notify Surveillance Center» yoqilgan bo'lishi kerak.
2. NVR'da faqat o'qish huquqiga ega alohida foydalanuvchi oching.
3. Panel → Xonalar va zonalar: avval kameralarni (NVR kanal raqami bilan) kiriting, keyin har bir kanal va hudud raqamini (regionID) xonaga bog'lang.
4. Mehmonxona lokal tarmog'idagi mini-PC'da (Linux, Python 3.11+):

```bash
pip install httpx
cp edge/config.example.toml edge/config.toml && nano edge/config.toml
python -m edge.agent --config edge/config.toml      # systemd xizmati sifatida ishga tushiring
```

NVR internetga ochilmaydi: agent faqat chiquvchi HTTPS ulanishdan foydalanadi. Internet uzilsa, hodisalar `edge.queue.db` faylida saqlanib turadi va aloqa tiklangach yuboriladi.

## 7. QR plitalar

Panel → Xonalar → «QR plitalarni chop etish» (10×15 sm). Plita yo'qolsa yoki nusxalangan bo'lsa, «Yangilash» tugmasi eski kodni bekor qiladi.
