# Рефакторинг створення бронювання (KAN-7 / A4)

## Що зроблено
Один рефакторинг «Extract Function» у `booking/rooms.py`, поведінка не змінюється.

| До | Після |
|---|---|
| `create(request)` — один view: розбір JSON, перевірка `room_id` та інтервалу, транзакція з `select_for_update` кімнати, `Booking.objects.create(..., status=ACTIVE)`, перетворення `IntegrityError` у 409 | `parse_booking_payload(raw_body)` → `(room_id, start, end)` або `None` (чиста функція, без БД) |
| | `reserve(owner, room_id, start, end)` → `Booking` або `None` (кімнати немає); блок `transaction.atomic()` з блокуванням рядка кімнати; конфлікт виходить як `IntegrityError` після відкату |
| | `create(request)` — лише відображення результатів у HTTP: `None` з парсера → 400 `invalid_input`, `None` з `reserve` → 404 `not_found`, `is_overlap_violation` → 409 `slot_conflict`, інакше 201 |

`parse_instant`, `parse_interval`, `overlapping`, `available`, `is_overlap_violation` і `booking/rooms_urls.py` не змінено (маршрут і далі вказує на `rooms.create`).

## Навіщо
- У v2 (`docs/change.md`, §3) у `create` має змінитися лише вибір статусу за `room.capacity`. Тепер це одне місце — `reserve`, де вже є заблокований `room`; валідацію і HTTP-мапінг це не зачіпає.
- Валідацію можна тестувати без БД і HTTP.
- `IntegrityError` і далі перехоплюється поза `atomic()`, тому транзакція відкочується до формування 409, як і раніше.

## Чому поведінка збережена
- Перевірки ті самі й ідуть у тому самому порядку. Усі помилки валідації дають одну відповідь 400, тому повернення `None` нічого не втрачає.
- 404 для відсутньої кімнати, як і раніше, повертається без INSERT. Раніше `return` усередині `atomic()` фіксував порожню транзакцію, тепер `None` повертається з тієї самої порожньої транзакції.
- Блокування рядка кімнати, статус `ACTIVE` і форма `record(row)` лишилися без змін.

## Тести, що це фіксують (без змін)
- Характеризаційні: `booking/tests/test_legacy.py` — `V1OnR100.test_create_returns_active_record`, `test_conflict_and_adjacent`, `test_invalid_input_and_missing_room`, `test_search_cancel_lifecycle`, `test_unauthenticated_is_401` і весь `LegacyActiveOnR101`.
- `booking/tests/test_create.py` — `CreateBooking.test_malformed_body_is_400`, `test_invalid_fields_are_400`, `test_unknown_room_is_404`, `test_overlap_same_room_conflicts`, `test_other_integrity_errors_are_not_overlap`; `ConcurrentCreate.test_simultaneous_same_slot_one_wins`, `test_simultaneous_partial_overlap_one_wins`, `test_simultaneous_adjacent_both_win`.
- Спільні `qa/test_contract.py`.

## Не входить у цю задачу
Статус `PENDING` для `capacity > 100`, approvals і міграції v2 — це наступні задачі.
