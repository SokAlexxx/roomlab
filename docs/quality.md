# План якості RoomLab (KAN-13 / B3)

Джерело правди щодо поведінки — `docs/contract.md`. Цілі якості й початкові ризики — `docs/team.md`. Тут зафіксовано, як саме кожну ціль і ризик перевіряють наявні автоматичні тести.

## Рівні перевірки

| Рівень | Де | Що перевіряє |
|--------|----|--------------|
| Статичний | Ruff, `manage.py check`, `makemigrations --check` | Стиль/помилки коду, конфігурація Django, відсутність незафіксованих змін моделей |
| Модульні / API | `booking/tests/test_search.py`, `test_create.py`, `test_flow.py`, `test_seed.py` | Кожен ендпоінт окремо: формат, коди помилок, межі `[start,end)`, права, ідемпотентність seed |
| Конкурентність | `booking/tests/test_create.py::ConcurrentCreate` | Реальні паралельні транзакції PostgreSQL (`TransactionTestCase`, потоки + бар'єр) |
| E2E v1 | `booking/tests/test_e2e.py::V1EndToEnd` | Повний сценарій через URLconf, middleware і view: створення → список → чуже скасування 403 → скасування → повторне створення |
| Телеметрія | `booking/tests/test_e2e.py::TelemetryLogging`, `TelemetryOnException` | Структурований лог запиту без облікових даних |
| Контракт (спільний) | `qa/test_contract.py` | Незмінні тести команди; активуються файлами `evidence/*.md` |

Усі тести виконуються на PostgreSQL у одноразовому Docker-стеку командою `py -3.12 .roomlab-check.py` (те саме — у CI). SQLite не використовується: exclusion-обмеження і `SELECT … FOR UPDATE` потребують PostgreSQL.

## Відповідність ризиків тестам

| # | Ризик (з `docs/team.md`) | Захід | Тести, що його перевіряють |
|---|--------------------------|-------|----------------------------|
| 1 | Подвійне бронювання при конкурентних запитах | Exclusion-обмеження PostgreSQL на `(room, tstzrange)` | `test_create.py::ConcurrentCreate::test_simultaneous_same_slot_one_wins`, `test_simultaneous_partial_overlap_one_wins`, `test_simultaneous_adjacent_both_win`; `CreateBooking::test_database_rejects_overlap_without_api`, `test_constraints_present_in_database`; `qa/test_contract.py::test_postgres_allows_only_one_booking`; крок 409 у `test_e2e.py::V1EndToEnd` |
| 2 | Обхід прав (скасування чужим) | ADR-2: власник лише з `request.user`, перевірка до мутації | `test_flow.py::test_foreign_cancel_is_403_without_mutation`, `test_foreign_inactive_is_still_403`, `test_cancel_requires_authentication`, `test_mine_owner_isolation`; `test_create.py::test_owner_comes_from_server_not_body`, `test_requires_authentication`; крок 403 зі знімком рядка в `test_e2e.py::V1EndToEnd`; `qa/test_contract.py::test_owner` |
| 3 | Неідемпотентний seed | `get_or_create` за унікальними ключами, без видалень | `test_seed.py::test_rerun_is_idempotent_and_preserves_ids_and_bookings`, `test_keeps_preexisting_rows`, `test_rerun_resets_passwords_and_flags`; `qa/test_contract.py::test_seed` |
| 4 | Міграція v2 ламає legacy-записи | Лише адитивні міграції, збереження constraints | Частково: `makemigrations --check` у перевірці; `test_create.py::test_constraints_present_in_database`. Тест на знімку legacy-рядків після міграції v2 **ще не існує** — з'явиться разом із v2 |
| 5 | Витік секретів | Секрети лише через змінні середовища; не логуються | `test_e2e.py::TelemetryLogging::test_no_credentials_or_request_data_logged`, `test_unsafe_client_request_id_is_replaced` (пароль, Basic-заголовок, ім'я користувача не потрапляють у лог). Відсутність секретів у репозиторії перевіряється лише рев'ю диффів |

## Цілі якості

| Ціль | Стан автоматичної перевірки |
|------|-----------------------------|
| Доступ: 0 несанкціонованих мутацій | Покрито тестами ризику 2 (знімок рядка до/після відмови) |
| Відновлення < 5 хв | Автоматичного тесту немає; ручна процедура `pg_dump`/`pg_restore` описана в `docs/team.md` |
| p95 пошуку ≤ 1 с | Автоматичного тесту немає. `elapsed_ms` у лозі дає сирі дані для розрахунку p95 на демо-стенді |

## Спостережуваність

Middleware `booking.telemetry.RequestTelemetry` (єдиний у `MIDDLEWARE`) пише на логер `roomlab.request` один JSON-рядок на запит:

```json
{"time":"2030-01-01T10:00:00.123+00:00","operation":"POST bookings-create","status":201,"elapsed_ms":4.217,"request_id":"9f1c…"}
```

- `operation` — метод + ім'я маршруту (не шлях, не query string); невідомий шлях → `unresolved`.
- `request_id` — клієнтський `X-Request-ID`, якщо відповідає `[A-Za-z0-9._-]{1,64}`, інакше UUID4; повертається в заголовку відповіді.
- Виняток у view логується як `500` і передається далі.
- Не логуються заголовки, тіло, query string, ім'я користувача, пароль.

## Критерії готовності зміни

1. `py -3.12 .roomlab-check.py` завершується `ROOMLAB_CHECK_OK`.
2. Нова поведінка має тест; зміна прав — тест «відмова без мутації».
3. Спільні `qa/` тести не змінено.
