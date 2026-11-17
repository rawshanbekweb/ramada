# Ega (rahbar) uchun qo'llanma

Siz Telegram bot va veb-panel orqali ishlaysiz.

## Har kuni

- **09:00 da kunlik hisobot** Telegramga keladi. Unda:
  - bandlik va tushum;
  - 3/3 ulushi va to'xtab qolgan bronlar;
  - ogohlantirishlar va kassa;
  - kirishlar soni.
- **Darhol keladigan xabarlar** (yuqori va kritik):
  - R2/R3 — bronsiz yoki boshqa xonaga kirish;
  - R5 — mehmon «boshqa summa to'ladim» dedi;
  - R9 — kassada farq;
  - R10 — to'lovdan keyin bekor qilish;
  - kamera yopildi yoki oflayn;
  - tasdiqsiz kalit berishga urinish.
- Har bir xabar ostida: «🎥 Video», «👁 Ko'rdim», «✅ Hal qilindi», «🚫 Noto'g'ri signal».
  - Noto'g'ri signallarni albatta belgilang: shu ma'lumot bilan kamera zonalari sozlanadi.

## Tasdiqlar (botda tugma bilan)

- **Chegirma:** belgilangan chegaradan kichigini administrator, kattasini siz tasdiqlaysiz. 10 daqiqada javob bo'lmasa, rad etiladi.
- **Bekor qilish so'rovi** — siz yoki administrator.
- **«Keyin to'lash»** — faqat siz.

Botdagi «📝 So'rovlar» tugmasi kutilayotgan barcha so'rovlarni ko'rsatadi.

## Veb-panel (https://DOMEN/panel)

| Bo'lim | Nima uchun |
|---|---|
| Bandlik | 40 xona jadvali: 3/3, 2/3, 1/3, kutilmoqda |
| Bronlar | Har bir bronning to'liq tarixi: to'lovlar, kamera, QR, jurnal |
| Ogohlantirishlar | Barcha signallar, izoh bilan yopish |
| Kameralar | Kameralar holati, kuzatuv %, eshik zonalarini chizish, sozlash rejimi |
| Hisobotlar | Kunlik, haftalik (xodimlar jadvali), oylik (KPI), Excel |
| Tariflar | **Faqat siz:** toifa × kun turi × mavsum, bayramlar |
| Sozlamalar | Wi-Fi paroli, kirish oynasi, chegirma chegarasi, soatlar |
| Xodimlar | Qo'shish, taklif kodi, o'chirish |
| Jurnal | Kim, qachon, nimani o'zgartirgani (o'chirib bo'lmaydi) |

## Kassadan pul olish (inkassatsiya)

Kassadan tushumni olsangiz, albatta yozing: botda «💰 Inkassatsiya» → smenani tanlang → summani kiriting. Panelda ham xuddi shunday: Smenalar → «Inkassatsiya».

Yozilmasa, smena yopilganda yoki keyingi smena qabul qilinganda yolg'on «kassa farqi» (R9) chiqadi. Reception'ga ham avtomatik xabar boradi.

## Muntazam ishlar

- **Wi-Fi paroli:** oyiga bir marta almashtiring (Sozlamalar). Mehmonlar uni faqat QR orqali oladi.
- **Haftalik hisobot (dushanba):**
  - qaysi xodimda 3/3 ulushi past;
  - kimda smartfonsiz mehmonlar yoki chegirmalar ko'p;
  - kimda kassa farqi bor.
- **Oyiga bir marta** «sirli mehmon» tekshiruvi.
- **Xodim ishdan ketsa:** Xodimlar → «O'chirish». Uning Telegram bilan bog'lanishi darhol uziladi.
- **Panelda «⚠️ Worker ishlamayapti» chiqsa:** ogohlantirishlar va bot to'xtagan. Texnik xodimga ayting (`docker compose restart worker`).
- **QR plita yo'qolsa yoki nusxalangan deb gumon qilinsa:** Xonalar → «Yangilash». Eski QR yaroqsiz bo'ladi, yangisini chop eting.
