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

Kamera — tizimning eng muhim qismi. Ishonchli bo'lishi uchun NVR quyidagicha sozlanadi.

### 5.1. NVR sozlamalari (har bir koridor kamerasi)

| Nima | Qayerda (Hikvision) | Nega |
|---|---|---|
| **Bostirib kirish (Intrusion)** har bir eshik oldida, *Duration (vaqt chegarasi) = 2–3 soniya* | Configuration → Event → Smart Event → Intrusion Detection | Odam eshik oldida to'xtab, eshikni ochadi — bu **kuchli dalil**. Yonidan o'tib ketgan odam hodisa bermaydi |
| **Hududga kirish (Region Entrance)** — xuddi o'sha hudud | Smart Event → Region Entrance | Qo'shimcha (kuchsiz) dalil: 2-bosqichni tezroq tasdiqlaydi |
| Hudud raqami (Region No.) | Har bir eshik uchun alohida 1, 2, 3… | Server shu raqam orqali eshikni xonaga bog'laydi |
| Nishon turi: **Human** (AcuSense kameralarda) | Target Type / Detection Target | Mushuk, yorug'lik, arava kabi narsalar hisobga olinmaydi |
| **Video Tampering** va **Video Loss** | Configuration → Event → Basic Event | Kamera yopilsa yoki o'chsa, egaga darhol xabar boradi |
| «Notify Surveillance Center» | Har bir hodisaning Linkage Method bo'limida | Hodisa edge agentga yetib borishi uchun |
| **NTP** (vaqt sinxronizatsiyasi) | System → Time Settings | Vaqt oynalari aniq ishlashi uchun. Tizim soat farqini ko'rsa ham ogohlantiradi |
| Asosiy kirishga **Line Crossing** | Smart Event → Line Crossing | Kunlik hisobotdagi kirishlar soni uchun |

NVR'da faqat o'qish huquqiga ega alohida foydalanuvchi oching (ISAPI uchun).

### 5.2. Edge agent (mehmonxona lokal tarmog'idagi mini-PC, Linux, Python 3.11+)

```bash
pip install httpx
cp edge/config.example.toml edge/config.toml && nano edge/config.toml
python -m edge.agent --config edge/config.toml      # systemd xizmati sifatida ishga tushiring
```

NVR internetga ochilmaydi: agent faqat chiquvchi HTTPS ulanishdan foydalanadi. Internet uzilsa, hodisalar `edge.queue.db` faylida saqlanib turadi va aloqa tiklangach yuboriladi. Agent NVR oqimi uzilgan davrlarni, shu jumladan o'zi ishlamagan davrni ham qayd etib, serverga yuboradi.

### 5.3. Zonalarni bog'lash (audit, 1-hafta)

1. Panel → **Kameralar** → «Zona sozlash rejimi»ni 2 soatga yoqing. Bu vaqtda bronsiz xonadagi harakat ogohlantirilmaydi.
2. Bir kishi har bir eshik oldida 3–4 soniya turadi. Sahifa har 5 soniyada yangilanadi, «Bog'lanmagan hududlar» jadvalida kanal va hudud raqami paydo bo'ladi.
3. Shu qatorda xonani tanlab, «Bog'lash» tugmasini bosing.
4. 40 ta eshik tugagach, rejimni o'chiring. Rejim 4 soatdan keyin o'zi ham o'chadi.

### 5.4. Tizim kameradan nimani kutadi

- **Kuchli dalil** (intrusion yoki bir eshikda 90 soniya ichida 2+ hodisa) bo'lsa, R2/R3 darhol egaga boradi.
- **Kuchsiz dalil** (bitta qisqa hodisa) 3 daqiqa ichida tasdiqlanmasa, faqat o'rta darajada hisobotga tushadi.
- Kamera yopilsa, signali yo'qolsa yoki oflayn bo'lsa, egaga **darhol** (yuqori daraja) xabar boradi. Shu kamera ko'rmagan oynalar uchun noto'g'ri R1 chiqmaydi.
- 24 soat davomida hodisa bermagan kamera R12 ogohlantirishini beradi: NVR'da hudud aniqlash o'chirilgan bo'lishi mumkin.
- Panel → Kameralar sahifasida har bir kamera uchun kuzatuv foizi va 30 kunlik noto'g'ri signallar soni ko'rinadi. 3 tadan ko'p bo'lsa, shu kameraning hududi yoki sezgirligini qayta sozlang.

## 6. QR plitalar

Panel → Xonalar → «QR plitalarni chop etish» (10×15 sm). Plita yo'qolsa yoki nusxalangan bo'lsa, «Yangilash» tugmasi eski kodni bekor qiladi.
