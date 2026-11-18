# O'z kompyuteringizda ishga tushirish

Tizimni ko'rish va sinab ko'rish uchun server, domen, Telegram yoki haqiqiy kamera kerak emas. Bitta buyruq quyidagilarni ishga tushiradi:
- ega paneli, **2 haftalik demo ma'lumotlar** bilan;
- **virtual kameralar** — tugma bosib «odam»ni eshik oldiga qo'yasiz;
- edge agent: kamera tasvirini xuddi mehmonxonadagi mini-PC kabi tahlil qiladi;
- fon jarayoni (worker): ogohlantirish qoidalari va hisobotlar.

Avval loyihani yuklab oling: GitHub'da **rawshanbekweb/ramada** → yashil **Code** tugmasi → **Download ZIP** → arxivni oching. Git bilan ishlasangiz: `git clone https://github.com/rawshanbekweb/ramada.git`.

---

## A variant — Docker Desktop orqali (eng oson, Python kerak emas)

1. **Docker Desktop**ni o'rnating (docker.com, Windows/macOS) va ishga tushiring.
2. Loyiha papkasida terminal oching. Windows'da papka ichida bo'sh joyga Shift + o'ng tugma → «Terminalda ochish».
3. Buyruqni kiriting:
   ```
   docker compose -f docker-compose.local.yml up --build
   ```
   Birinchi marta 3–5 daqiqa davom etadi. Terminalda «✅ Shaffof qabul (Ramada) lokal ishga tushdi» chiqqach, tayyor.
4. Brauzerda oching:
   - Panel: **http://localhost:8000/panel/** — login `owner`, parol `ramada123`
   - Virtual kameralar: **http://localhost:8601/**

To'xtatish — terminalda Ctrl+C. Ma'lumotlarni o'chirib, qaytadan boshlash:
```
docker compose -f docker-compose.local.yml down -v
```

## B variant — Python orqali

1. Python 3.12 ni o'rnating (python.org). **Windows'da o'rnatishda «Add python.exe to PATH» belgisini qo'ying.**
2. Ishga tushiring:
   - **Windows:** loyiha papkasidagi `start.bat` ni ikki marta bosing.
   - **macOS / Linux:** terminalda `./start.sh`
3. Birinchi marta kutubxonalar o'rnatiladi (bir necha daqiqa), keyin brauzerda panel o'zi ochiladi.

Qaytadan boshlash uchun: `start.bat --reset` yoki `./start.sh --reset`. Demo ma'lumotlarsiz, bo'sh baza bilan: `--empty`.

---

## Nimani sinab ko'rish kerak

Terminalda **sinov mehmoni** uchun tayyor havolalar chiqadi. Shu tartibda bosing:

| Qadam | Nima qilasiz | Natija |
|---|---|---|
| 1 | Mehmon sahifasini oching → **«Tasdiqlayman»** | Mehmon narxni o'z telefonida tasdiqladi |
| 2 | Panel → Bronlar → shu bron → **«💵 To'lovni saqlash»** (chek raqamini yozing) | Bron **1/3**, kirish oynasi ochildi |
| 3 | Virtual kameralar → **«101-xona oldida turish»** | Bir necha soniyada bron **2/3** |
| 4 | Xona QR havolasini oching → **«Ha, to'g'ri»** | Bron **3/3**, Wi-Fi paroli ko'rinadi |

Keyin buzilish holatlarini sinang:

- **Bo'sh xona oldida turish** (masalan, 104): Ogohlantirishlar bo'limida **R2** chiqadi, kamera kadri bilan.
- **Boshqa xonaga kirish:** to'langan bron bor paytda boshqa bo'sh xona oldida turish → **R3 (kritik)**.
- **«Koridordan o'tib ketish»:** yuqori darajadagi signal bo'lmasligi kerak.
- **«Chiroq o'chdi»:** hech qanday signal bo'lmasligi kerak.
- **«Kamerani yopish» / «Kamera o'chdi»:** **R12**. Panel → Kameralar bo'limida holat va uzilish davri ko'rinadi.
- **Panel → Kameralar → «✏️ zonalar»:** eshik zonalarini kadr ustida qanday chizishni ko'ring.
- **Hisobotlar:** kunlik, haftalik (xodimlar jadvali), oylik KPI, Excel eksport.

## Mehmon sahifasini telefonda ko'rish

Kompyuter va telefon bir Wi-Fi tarmog'ida bo'lishi kerak.

1. Ishga tushiring: `start.bat --host 0.0.0.0` (Docker variantida bu sozlama allaqachon yoqilgan).
2. Telefonda `http://KOMPYUTER_IP:8000/g/...` manzilini oching. Kompyuter IP'sini Windows'da `ipconfig` buyrug'i ko'rsatadi.
3. Windows xavfsizlik devori (firewall) ruxsat so'rasa, «Ruxsat berish» ni bosing.

## Lokalda nima ishlamaydi

- **Telegram bot.** Bot tokeni kerak. Reception ishlarini (to'lov kiritish, smena) panel orqali bajarasiz.
- **SMS.** SMS'lar yuborilmaydi, faqat `local/logs/web.log` fayliga yoziladi.
- **Haqiqiy kamera.** Ulamoqchi bo'lsangiz: `local/edge.toml` fayliga kameraning RTSP manzilini yozing (qarang: `edge/config.example.toml`). Manzilni avval tekshiring: `python -m edge.check "rtsp://..."`.

## Muammo bo'lsa

- **«port band» xatosi:** `start.bat --port 8080 --cam-port 8602` bilan boshqa portda ishga tushiring.
- **Biror jarayon to'xtab qolsa:** `local/logs/` papkasidagi loglarni ko'ring (`web.log`, `worker.log`, `edge.log`, `camsim.log`).
