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

## 3. To'lovlar

Onlayn to'lov yo'q. Reception botda «💵 Naqd» yoki «💳 Terminal» tugmasini bosib, summani va fiskal chek raqamini kiritadi. Mehmon baribir o'z telefonida summani tasdiqlaydi; ikkalasi ham bo'lmaguncha bron 1/3 ga o'tmaydi.

## 4. SMS (eskiz.uz)

`SQ_SMS_PROVIDER=eskiz`, login va parol kiritiladi. SMS shablonlarini eskiz'da oldindan tasdiqlatib olish kerak (matnlar `app/i18n/messages.py` faylida, `sms.*` kalitlari ostida).

## 5. Kameralar va edge agent

Tizim kamera brendiga bog'liq emas va «aqlli» (AI, AcuSense) kameralarni talab qilmaydi. Eshik oldidagi harakat tahlili mehmonxonadagi mini-PC'da bajariladi. Kameradan faqat bitta narsa kerak: **RTSP video oqimi**.

### 5.1. Kamera tanlash (oddiy, arzon)

| Talab | Nega |
|---|---|
| IP kamera, **RTSP** va ONVIF qo'llab-quvvatlashi | Deyarli barcha IP kameralarda bor. Tahlil uchun RTSP yetarli |
| 2 MP (1080p), **substream** (640×360 yoki 704×576) | Tahlil kichik oqimda bajariladi va mini-PC'ni kam yuklaydi |
| Ob'ektiv 2.8–4 mm, IK (tungi) yoritish | Koridorni to'liq qamraydi, kechasi ham ishlaydi |
| PoE (tarmoq kabeli orqali quvvat) | O'rnatish va zaxira quvvat (UPS) oson |
| Fisheye yoki «aqlli» funksiyalar **kerak emas** | Arzon modellar yetarli |

Mavjud kameralar (Hikvision, Dahua, XMEye, Tp-Link, Ezviz va boshqalar) RTSP berar ekan, yangisini olish shart emas.

**Joylashuv:** kamera koridor oxirida, shiftga yaqin joyda turib, eshiklar qatori bo'ylab qaraydi. Uzun koridorda (12+ eshik) uzoqdagi eshiklar kichik ko'rinadi — ikki tomondan ikkita kamera qo'yish tavsiya etiladi. Har bir eshik oldidagi pol qismi kamerada ochiq ko'rinishi kerak.

### 5.2. Mini-PC (edge)

- 4 yadroli oddiy mini-PC (masalan, Intel N100 sinfi), 8 GB RAM, SSD, Linux. Shunday mini-PC 4–8 ta koridor kamerasini (sekundiga 5 kadr) bemalol tortadi.
- Kameralar bilan bir lokal tarmoqda, simli ulanish, UPS.
- Kamera va NVR internetga ochilmaydi. Agent faqat serverga chiquvchi HTTPS ulanishdan foydalanadi.

```bash
pip install httpx numpy opencv-python-headless
cp edge/config.example.toml edge/config.toml && nano edge/config.toml
python -m edge.agent --config edge/config.toml      # systemd xizmati sifatida ishga tushiring
```

### 5.3. Ulash usullari (`edge/config.toml`, `[[sources]]`)

| Tur | Qachon ishlatiladi | Eshik darajasida aniqlik |
|---|---|---|
| `rtsp` (**tavsiya**) | Istalgan IP kamera, NVR kanali ham bo'ladi | Ha — zonalar panelda chiziladi, tahlil mini-PC'da |
| `hikvision` | Hikvision'da Intrusion/Region Entrance sozlangan bo'lsa | Ha — kamera o'zi aniqlaydi |
| `dahua` | Dahua va OEM'larida (Imou, Amcrest…) Intrusion sozlangan bo'lsa | Ha — hudud nomi «1», «2»… bo'lishi kerak |
| `onvif` | Kamerada ONVIF hudud qoidalari bo'lsa | Faqat hudud qoidasi bo'lsa; oddiy «Motion» butun kadr uchun |

Bir tizimda turlarni aralashtirish mumkin. RTSP manzillari misollari `edge/config.example.toml` faylida.

### 5.4. Eshik zonalarini belgilash (audit, 1-hafta)

**RTSP kameralar:**
1. Agent ishga tushgach, kamera kadri bir necha daqiqada panelga keladi.
2. Panel → **Kameralar** → kamera yonidagi «✏️ zonalar» havolasini bosing.
3. Kadr ustida har bir eshik oldidagi pol qismini 4–6 nuqta bilan belgilang, xonani tanlab, «Saqlash» tugmasini bosing.
4. Odam eshik ochishda turadigan joyni oling, koridorning o'rtasini emas. Shunda yonidan o'tganlar kamroq hisoblanadi.

**Kamera o'zi aniqlaydigan turlar (hikvision/dahua/onvif):**
1. Panel → Kameralar → «Zona sozlash rejimi»ni yoqing.
2. Bir kishi har bir eshik oldida 3–4 soniya turadi.
3. Paydo bo'lgan kanal va hudud raqamini xonaga bog'lang.

Ikkala holatda ham sinov: har bir eshik oldida 3 soniya turing va «Oxirgi hodisalar» jadvalida shu xona «kuchli» dalil bilan chiqishini tekshiring. Keyin yonidan o'tib keting — «kuchsiz» chiqishi yoki umuman chiqmasligi kerak.

### 5.5. Tizim nimani aniqlaydi

- **Kuchli dalil:** odam eshik oldida ~2.5 soniyadan ko'p turdi yoki 90 soniya ichida takroriy harakat bo'ldi. R2/R3 darhol egaga boradi.
- **Kuchsiz dalil:** bitta qisqa harakat (yonidan o'tgan odam). 3 daqiqada tasdiqlanmasa, faqat o'rta darajada hisobotga tushadi.
- **Kamera sog'lig'i** (egaga darhol):
  - kamera yopildi — tasvir qorong'i yoki bir xil;
  - kamera burildi — sahna uzoq vaqt o'zgarib qoldi;
  - RTSP oqimi uzildi yoki kamera oflayn.
  - 24 soat davomida hodisa bermagan kamera haqida ham ogohlantirish chiqadi.
- Kamera ishlamagan davrdagi mehmonlar uchun noto'g'ri R1 chiqmaydi: 2-bosqich «tekshirib bo'lmadi» deb belgilanadi.
- Panel → Kameralar sahifasida har bir kameraning kuzatuv foizi va 30 kunlik noto'g'ri signallar soni ko'rinadi. Signallar ko'p bo'lsa, zonani toraytiring yoki `dwell_seconds` ni oshiring.

**Cheklov:** oddiy harakat tahlili odamni hayvon yoki soyadan ajrata olmaydi. Shuning uchun yuqori daraja faqat kuchli dalil bilan beriladi, tungi yoritish esa bir xil bo'lishi kerak.

## 6. QR plitalar

Panel → Xonalar → «QR plitalarni chop etish» (10×15 sm). Plita yo'qolsa yoki nusxalangan bo'lsa, «Yangilash» tugmasi eski kodni bekor qiladi.
