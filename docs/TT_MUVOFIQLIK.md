# TT talablari va kod

Har bir funksional talab kodning qaysi joyida bajarilgani va qaysi test bilan tekshirilgani quyida keltirilgan.

## Bron va tasdiq holatlari (7.1)

| ID | Qayerda | Test |
|---|---|---|
| FT-01 | `models.Room`, `CameraZone`; panel → Xonalar | test_web::test_panel_pages |
| FT-02 | `services/bookings.py`: `evaluate_stage1`, `refresh_stage`, `Confirmation` | test_acceptance 3, 6 |
| FT-03 | `bookings.create_booking`: surat va telefonsiz bron yaratilmaydi | test_bot |
| FT-04 | `bookings.stage1_ready`: to'lov yoki ega tasdiqlagan «keyin to'lash» | test_flows::test_pay_later_owner_only |
| FT-05 | O'chirish yo'q; `bookings.cancel`, `request_cancel` va R10 | test_flows::test_cancel_after_payment_r10 |
| FT-06 | `bookings.extend`, `change_room`, `Amendment` (yangi tasdiq va to'lov, yangi kirish oynasi) | test_flows::test_extension, test_room_change |
| FT-07 | Panel → Bandlik (1/3, 2/3, 3/3 ranglari) | — |

## Tarif, chegirma, to'lov, kassa (7.2)

| ID | Qayerda | Test |
|---|---|---|
| FT-10 | `services/pricing.py`; panel → Tariflar (faqat ega, audit) | test_flows::test_pricing |
| FT-11 | `services/discounts.py`: chegaradan kichigini administrator, kattasini ega tasdiqlaydi, 10 daqiqada javob bo'lmasa rad etiladi | test_flows::test_discount |
| FT-12 | `pricing.early_checkin_line`, `late_checkout_line` | test_flows::test_checkout_with_late_fee |
| FT-13 | `integrations/payme.py`, `click.py`; summa mos kelmasa rad etiladi | test_payments |
| FT-14 | `bookings.record_payment`: chek raqami majburiy | test_acceptance 1 |
| FT-15 | `services/shifts.py`: ko'r hisob, smena qabulidagi farq | test_acceptance 11 |

## Mehmon sahifasi (7.3)

| ID | Qayerda |
|---|---|
| FT-20 | `Booking.guest_token`, 7 kunlik muddat, birinchi tasdiqlagan qurilmaga bog'lanadi |
| FT-21 | `templates/guest/booking.html` |
| FT-22 | Mobil sahifa, ro'yxatdan o'tish yo'q, kaa/uz/ru/en tillari (`app/i18n`) |
| FT-23 | `guestflow.scan`: imzolangan token, qurilma yoki oxirgi 4 raqam bilan, urinishlar soni cheklangan |
| FT-24 | `guestflow.answer`, `templates/guest/qr.html` |
| FT-25 | `rules.qr_reminders` (bir marta) |
| FT-26 | `guestflow.submit_survey`; narx shikoyati darhol egaga yuboriladi |

## Kamera (7.4)

| ID | Qayerda |
|---|---|
| FT-30 | `CameraZone`; panel → Xonalar va zonalar (faqat ega) |
| FT-31 | `edge/agent.py` (ISAPI alertStream) → `POST /api/edge/events` |
| FT-32 | `entry.open_window` (10..120 daqiqa), `_confirm_entry` + video havola |
| FT-33 | `entry.sweep_windows` (R1), `process_door_event` (R3) |
| FT-34 | `process_door_event` (R2), farrosh sessiyasi bo'lsa hisobga olinmaydi |
| FT-35 | `ZoneKind.ENTRANCE`, `entry.entrance_count`, kunlik hisobotda ko'rsatiladi |
| FT-36 | `entry.check_edge` (R12) va heartbeat |

## Farrosh, ogohlantirish, hisobot (7.5)

| ID | Qayerda |
|---|---|
| FT-40 | `services/housekeeping.py`, bot `h_housekeeping.py` |
| FT-41 | `rules.run_minutely` (har daqiqa) va `run_every_15` |
| FT-42 | `notify.dispatch_alert`: tranzaksiya yakunlangach darhol, yuborilmasa qayta urinadi |
| FT-43–44 | `services/reports.py`, `worker.py` (09:00, dushanba, oy boshi) |
| FT-45 | `services/export.py`, panel → Excel |
| FT-46 (I) | `worker.keyboard_check`, bot foto qabul qiladi |
| FT-47 (I) | `housekeeping.log_linen`, `linen_balance` (haftalik hisobot) |

## Ogohlantirish qoidalari (8-bo'lim)

R1 `entry.sweep_windows` · R2/R3 `entry.process_door_event` · R4/R5 `guestflow` · R6/R8 `rules` · R7 `housekeeping.save_morning_check` · R9 `shifts` · R10 `bookings.cancel`, Payme qaytarish · R11 `reports.staff_outliers` · R12 `entry.check_edge`, `housekeeping.check_morning_missing`, QR plita.

Ikkita qo'shimcha kod bor: **KEY** (tasdiqsiz kalit berishga urinish, 16-bo'lim 1-sinov) va **PRICE** (so'rovnomadagi narx shikoyati).

## Qabul sinovlari (16-bo'lim)

12 ta sinovning barchasi `tests/test_acceptance.py` faylida avtomatlashtirilgan va SQLite hamda PostgreSQL'da o'tadi.
