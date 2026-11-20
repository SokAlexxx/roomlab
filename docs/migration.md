# Міграція схеми v1 → v2 (KAN-8 / A5)

Міграція `booking/migrations/0003_booking_decision_audit.py` лише розширює схему (expand-only).
Нова поведінка створення бронювань у цій задачі **не вмикається**: `reserve` і далі створює `ACTIVE`,
ендпоінтів погодження ще немає, нові поля ніхто не записує.

## Що змінюється

| Об'єкт | v1 (0002) | v2 (0003) |
|---|---|---|
| `decided_by` | — | FK на користувача, `NULL`, `PROTECT`, `related_name="booking_decisions"` |
| `decided_at` | — | `timestamptz NULL` |
| `decision` | — | `varchar(8) NULL`, `approve` / `reject` |
| `booking_start_before_end`, `booking_status_valid` | є | без змін |
| `booking_decision_valid` | — | `decision IS NULL OR decision IN ('approve','reject')` |
| `booking_decision_audit_complete` | — | три поля аудиту або всі `NULL`, або всі заповнені |
| `booking_no_overlap_active` | `WHERE status = 'ACTIVE'` | `WHERE status IN ('ACTIVE','PENDING')` |

* Ім'я exclusion constraint збережено: `booking.rooms.is_overlap_violation` мапить на 409 саме його.
* `RemoveConstraint` + `AddConstraint` виконуються в одній транзакції міграції (DDL у PostgreSQL
  транзакційний), тож проміжку без захисту від перетинів немає.
* Дані не переписуються: legacy-записи зберігають `id`, власника, кімнату, `[start, end)` і статус;
  поля аудиту для них `NULL`. Ретроактивного погодження немає (наявні `ACTIVE` на R101 не стають `PENDING`).
* Застосування на наявних даних безпечне, бо v1 не створює `PENDING`, а `ACTIVE` уже не перетинаються.
  Якщо в БД вручну потрапили перетинні `PENDING`/`ACTIVE`, `migrate` впаде з помилкою exclusion
  constraint і відкотиться повністю; такі записи треба розібрати до міграції.

## Відкат (`migrate booking 0002`) і його межі

* Reverse автоматичний: видаляє дві check-обмеження аудиту, повертає overlap-constraint на `ACTIVE`,
  видаляє три колонки. Legacy-записи (`ACTIVE`, `CANCELLED`) зберігаються без змін — це перевіряє тест.
* **Втрата даних:** значення `decided_by`, `decided_at`, `decision` зникають безповоротно. Якщо аудит
  потрібен, його треба вивантажити до відкату (`dumpdata` або SQL-копія колонок).
* **Ослаблення цілісності:** після відкату `PENDING` більше не займає слот на рівні БД; v1-код (`rooms.overlapping`)
  ще враховує `PENDING` у пошуку, але конкурентний `INSERT` уже не блокується constraint. Тому перед
  відкатом `PENDING` треба перевести в `CANCELLED` (див. `docs/change.md`), а `REJECTED` можна лишити.
* **Повторне застосування** 0003 після відкату впаде, якщо за час роботи v1 з'явилися перетини
  `PENDING`/`ACTIVE` в одній кімнаті.
* Відкат коду v2 без відкату схеми безпечний: v1 ігнорує nullable-колонки, а ширший constraint не
  зачіпає v1, який створює лише `ACTIVE`.

## Revised ADR-1: exclusion constraint для `ACTIVE` і `PENDING`

**Статус:** прийнято, переглядає ADR-1 з `docs/architecture.md` (рішення B — constraint у БД — без змін).

**Контекст.** У v1 (A2) constraint покривав лише `ACTIVE`, бо інших блокувальних статусів не було. У v2
`PENDING` займає слот, а погодження/відхилення змінює статус конкурентно зі створенням.

**Рішення.** Розширити умову того самого constraint до `status IN ('ACTIVE','PENDING')` в окремій
міграції 0003, зберігши ім'я `booking_no_overlap_active`; аудит рішення зберігати в тому ж рядку
(`decided_by`, `decided_at`, `decision`) з check-обмеженням «все або нічого».

**Наслідки.**
* Набір блокувальних статусів у БД збігається з `rooms.BLOCKING_STATUSES`; пошук і створення
  використовують одне правило.
* Approve `PENDING → ACTIVE` лишається всередині блокувального набору і не може конфліктувати;
  reject/cancel виводять запис із набору й одразу звільняють слот.
* Ім'я constraint тепер неточне («active»), але його перейменування зламало б мапінг 409 і тести;
  свідомо залишено.
* Атомарність запису аудиту разом зі статусом забезпечуватиме код рішення (наступна задача) в одній
  транзакції; БД гарантує лише повноту трійки полів, а не відповідність `decision` статусу.

## Перевірка

`booking/tests/test_migration.py` (`TransactionTestCase` + `MigrationExecutor`): записи створюються
історичною моделлю стану 0002, застосовується 0003, порівнюються `id`, `owner_id`, `room_id`, `start`,
`end`, `status` для `ACTIVE` і `CANCELLED`; перевіряються новий overlap для `PENDING`, check-обмеження
аудиту і відкат на 0002. `tearDown` повертає останню схему.
