# Reception uchun qo'llanma

Barcha ish Telegram botda bajariladi. Bot birinchi marta ishga tushganda unga rahbar bergan kodni yuboring: `/start KOD`.

## Smena

**Smena ochilmaguncha mehmon joylashtirib ham, to'lov qabul qilib ham bo'lmaydi.**

- **Ochish:** «💵 Smena» → kassadagi naqd pulni sanab, summani yozing.
- **Yopish:** «💵 Smena» → «Smenani yopish» → kassadagi naqdni sanab, summani yozing.
- Tizim sizga «kutilgan» summani ko'rsatmaydi (ko'r hisob). Farq bo'lsa, rahbarga avtomatik xabar boradi. Shuning uchun naqdni aniq sanang.
- Smenani qabul qilganda oldingi xodim topshirgan summa bilan farq bo'lsa, u ham qayd etiladi.

## Yangi mehmon

1. «🆕 Yangi mehmon» → xonani tanlang. Ro'yxatda faqat bo'sh va toza xonalar chiqadi.
2. Tunlar sonini tanlang.
3. Mehmonning F.I.Sh., pasport seriya-raqami va **pasport surati** (majburiy).
4. Mehmonning **telefon raqami**. Unga shu raqamga SMS boradi.
5. «Smartfon bormi?»
   - **Ha** — oddiy tartib.
   - **Yo'q** — mehmon SMS havolasini ocholmaydi, siz faqat chek bilan to'lov kiritasiz, 3-bosqichni ertasi kuni farrosh tasdiqlaydi.
6. «Mehmon hozir shu yerdami?»
   - **Ha** — oddiy holat.
   - **Yo'q** — oldindan bron. Mehmon kelganda «🚶 Mehmon keldi» ni bosasiz.
7. «Chegirma kerakmi?»
   - Chegirma faqat rahbar tasdig'i bilan beriladi. Summa va sababini yozing.
   - 10 daqiqada javob bo'lmasa, chegirma rad etiladi.
   - SMS mehmonga qarordan keyin ketadi.
8. Ma'lumotni tekshirib, «✅ Tasdiqlash».

**Narxni siz kiritmaysiz va o'zgartira olmaysiz.** Narxni tizim tarif bo'yicha hisoblaydi. Erta kirish uchun qo'shimcha haq avtomatik qo'shiladi.

## Mehmon tasdiqlashi va to'lov

1. Mehmon SMS'dagi havolani ochadi, xona, muddat va narxni ko'radi va **«Tasdiqlayman»** ni bosadi. Mehmonga shunday deng: «Telefoningizga SMS keldi, ochib tasdiqlang».
2. To'lovni qabul qiling va **fiskal chek** bering.
3. Botda mehmon kartasi → «💵 Naqd» yoki «💳 Terminal» → summa → **chek raqami**. Chek raqamisiz to'lov qabul qilinmaydi.
4. Mehmon tasdig'i ham, to'liq to'lov ham bo'lgandan keyin bot yozadi: **«🔑 Kalitni bering»**.

**Muhim:**
- Tasdiq va to'lovsiz «🔑 Kalit berish» bosilsa, tizim rad etadi va rahbarga xabar beradi.
- Kam summa kiritilsa, bron tasdiqlanmaydi va rahbarga xabar ketadi.
- Mehmon har bir to'lov haqida SMS chek oladi va o'z sahifasida summani ko'radi.

## Kalitdan keyin

- **Kamera:** mehmon xonasiga 30 daqiqa ichida borishi kerak. Kamera buni o'zi qayd etadi (2/3).
- **Xonadagi QR:** mehmon xonadagi QR plitani skanerlab, Wi-Fi parolini oladi (3/3).
- **Wi-Fi parolini og'zaki aytmang** — u faqat QR orqali beriladi.

## Mehmonlar ro'yxati («📋 Mehmonlar»)

Holat belgilari: ⏳ — tasdiq kutilmoqda, 🟡 — 1/3, 🟢 — 2/3, ✅ — 3/3.

Mehmon kartasidagi tugmalar:

| Tugma | Nima qiladi |
|---|---|
| 💵 Naqd / 💳 Terminal | To'lov kiritish (chek raqami bilan) |
| 🔑 Kalit berish | Kalit berilganini belgilash |
| 🚶 Mehmon keldi | Oldindan bron qilgan mehmon keldi — kamera oynasi ochiladi |
| 📨 SMS qayta | Mehmonga havolani qayta yuborish |
| ➕ Uzaytirish | Qo'shimcha tunlar: mehmon yangi summani tasdiqlaydi va to'laydi |
| 🔁 Xona almashtirish | Yangi xona: mehmon tasdiqlaydi, farqni to'laydi, kamera yangi xonani tekshiradi |
| 🚪 Chiqish | Chiqarish. Kech chiqish haqi avtomatik hisoblanadi, qoldiq bo'lsa avval to'lanadi |
| 🕓 Keyin to'lash | Faqat rahbar ruxsati bilan |
| 🗑 Bekor qilish | Faqat sabab bilan, rahbar tasdig'i orqali. Bronni o'chirib bo'lmaydi |

## Tez-tez uchraydigan holatlar

- **Mehmonda smartfon yo'q:** bronni «smartfonsiz» deb yarating va to'lovni chek bilan kiriting.
- **Bir xonada bir nechta mehmon:** bron bitta telefonga yoziladi. Hamrohlar QR'ni shu telefonning oxirgi 4 raqami bilan tasdiqlaydi.
- **SMS kelmadi:** «📨 SMS qayta» ni bosing va raqam to'g'riligini tekshiring.
- **Mehmon «summa boshqa» desa:** mehmon sahifasidagi summa — rasmiy summa. Boshqa summa so'ralmaydi.

## Kalit taxtasi

Bot ba'zan kalit taxtasining suratini so'raydi. Darhol umumiy suratini yuboring.
