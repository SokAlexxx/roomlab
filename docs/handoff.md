# Передавання RoomLab (KAN-10 / A7)

Документ для того, хто підтримуватиме RoomLab після команди. Усе нижче взято з коду репозиторію
(`compose.yaml`, `Dockerfile`, `config/`, `booking/`, `.github/workflows/ci.yml`) і наявних документів
`docs/*.md`. Поведінку API визначає `docs/contract.md`. Результати окремих задач наведено в `evidence/*.md`, а фактичні перевірки передавання — у протоколі релізу v2.0.

Обсяг: лише локальне демо. Немає реєстрації, веб-інтерфейсу, e-mail, зовнішнього хостингу чи реальних даних.

## 1. Склад системи
| Частина | Де | Примітка |
|---|---|---|
| API (Django, `runserver` на 0.0.0.0:8000 у контейнері) | `Dockerfile`, `config/`, `booking/` | Сервер розробки, тільки для локального демо. Порт публікується лише на `127.0.0.1:${WEB_PORT:-8000}` |
| PostgreSQL 16 | сервіс `db` у `compose.yaml`, том `data` | Джерело істини. Healthcheck `pg_isready`, від якого залежить `web` |
| Маршрути | `config/urls.py`, `booking/rooms_urls.py`, `booking/flow_urls.py` | `/health/`, `/api/rooms/available/`, `/api/bookings/`, `/api/bookings/mine/`, `/api/bookings/{id}/cancel/`, `/api/approvals/`, `/api/approvals/{id}/decision/` |
| Міграції | `booking/migrations/0001…0003` | 0003 — аудит рішень і розширений overlap-constraint (див. §8) |
| Команди | `seed_demo`, `bench_search` | `bench_search` лише в одноразовій БД (`docs/operations.md`) |

Детальніше: `docs/architecture.md` (компоненти, ADR-1), `docs/change.md` (v1 → v2), `docs/decisions.md`
(стани й рішення staff), `docs/migration.md`, `docs/operations.md`, `docs/quality.md`, `docs/incident.md`.

## 2. Змінні середовища
| Змінна | Обов'язкова | Хто читає | Призначення |
|---|---|---|---|
| `PG_PASS` | так (`${PG_PASS:?}` у compose) | `db` (`POSTGRES_PASSWORD`), `web` (`config/settings.py`) | Пароль користувача БД `roomlab` |
| `DEMO_PASS` | так (`${DEMO_PASS:?}` у compose) | `web` → `seed_demo` | Спільний пароль синтетичних акаунтів. Без нього `seed_demo` падає й нічого не змінює |
| `DJANGO_SECRET_KEY` | так (`${DJANGO_SECRET_KEY:?}` у compose) | `config/settings.py` | Секрет Django |
| `WEB_PORT` | ні, типово `8000` | `compose.yaml` | Локальний порт хоста, наприклад `8001` для одноразового стеку |
| `DB_HOST` | ні, типово `db` | `config/settings.py` | Хост БД. Задають лише поза compose |

Значення в репозиторій не комітимо. `.env` і `*.dump` виключено з образу (`.dockerignore`). У CI
(`.github/workflows/ci.yml`) задано лише ефемерні тестові значення.

## 3. Синтетичні акаунти й дані
Їх створює `python manage.py seed_demo` (`booking/management/commands/seed_demo.py`):

| Об'єкт | Значення |
|---|---|
| Кімнати | `R100` (місткість 100 → бронювання одразу `ACTIVE`), `R101` (місткість 101 → `PENDING`, потребує рішення staff) |
| Користувачі | `alice`, `bob` — звичайні; `admin` — єдиний `is_staff`. `is_superuser` у всіх `False` |
| Пароль | однаковий для всіх, з `DEMO_PASS` |

Повторний запуск ідемпотентний: кімнати шукаються за унікальною назвою, ID і бронювання зберігаються,
паролі й прапорці скидаються до описаних вище. `bench_search` створює ще користувача `bench` і кімнати
`BENCH-*`, але лише в одноразовому стеку. У демо-базі їх бути не повинно.

## 4. Розгортання (Docker)
З кореня репозиторію, Git Bash або інший POSIX-shell:
```
export PG_PASS=<локальний пароль БД> DEMO_PASS=<пароль демо> DJANGO_SECRET_KEY=<випадковий рядок>
docker compose up -d --build                    # db чекає healthy, потім web
docker compose exec web python manage.py migrate --noinput
docker compose exec web python manage.py seed_demo
curl -s http://127.0.0.1:8000/health/           # очікується {"status": 200, "elapsed_ms": …} (config/urls.py)
```
Для v2.0 завантажте `roomlab-source.zip` з [релізу](https://github.com/SokAlexxx/roomlab/releases/tag/v2.0) або відкрийте тег `v2.0` у клоні. Для відтворення v1.0 використайте її архів або тег `v1.0`.

## 5. Тестування
- Задайте локальні змінні з [README](../README.md#локальне-демо). Виконайте кроки CI в окремому Compose-проєкті `roomlab-check`:
  ```
  docker compose -p roomlab-check build web
  docker compose -p roomlab-check up -d --wait db
  docker compose -p roomlab-check run --rm web ruff check .
  docker compose -p roomlab-check run --rm web python manage.py makemigrations --check --dry-run
  docker compose -p roomlab-check run --rm web python manage.py migrate --noinput
  docker compose -p roomlab-check run --rm web python manage.py test
  docker compose -p roomlab-check down -v
  ```
  Команда `down -v` видаляє том цього тестового проєкту.
  `manage.py test` використовує MD5-хешер і окрему тестову БД. SQLite не підтримується: exclusion constraint і
  `SELECT … FOR UPDATE` працюють лише в PostgreSQL.
- Спільні тести `qa/test_contract.py` змінювати не можна. Їхні етапи вмикаються наявністю `evidence/<KEY>.md`.
- Ручний замір p95 пошуку робиться через `bench_search` лише в окремому стеку `-p roomlab-bench` (`docs/operations.md`).

## 6. Smoke-перевірка після розгортання
Часи вказуються з явним зсувом. `+` у query string треба передавати як `%2B`; простіше писати `Z`.
```
B=http://127.0.0.1:8000; S=2030-01-01T10:00:00Z; E=2030-01-01T11:00:00Z
curl -s $B/health/                                                     # 200
curl -s -o /dev/null -w '%{http_code}\n' "$B/api/rooms/available/?start=$S&end=$E"   # 401 без автентифікації
curl -s -u alice:$DEMO_PASS "$B/api/rooms/available/?start=$S&end=$E"  # [R100, R101], за id
curl -s -u alice:$DEMO_PASS -X POST $B/api/bookings/ -H 'Content-Type: application/json' \
     -d '{"room_id":<id R100>,"start":"'$S'","end":"'$E'"}'             # 201 ACTIVE
curl -s -u bob:$DEMO_PASS -X POST $B/api/bookings/ -H 'Content-Type: application/json' \
     -d '{"room_id":<id R100>,"start":"'$S'","end":"'$E'"}'             # 409 slot_conflict
curl -s -u bob:$DEMO_PASS -X POST $B/api/bookings/ -H 'Content-Type: application/json' \
     -d '{"room_id":<id R101>,"start":"'$S'","end":"'$E'"}'             # 201 PENDING
curl -s -u admin:$DEMO_PASS $B/api/approvals/                          # масив із PENDING bob
curl -s -u admin:$DEMO_PASS -X POST $B/api/approvals/<id>/decision/ \
     -H 'Content-Type: application/json' -d '{"decision":"approve"}'   # 200 ACTIVE + decided_by/at, decision
curl -s -u alice:$DEMO_PASS -X POST $B/api/bookings/<id alice>/cancel/ \
     -H 'Content-Type: application/json' -d '{}'                        # 200 CANCELLED
curl -s -u alice:$DEMO_PASS $B/api/bookings/mine/                      # власні записи, зокрема CANCELLED
```
Smoke додає в демо-базу записи на 2030-01-01. Для чистого демо після нього виконайте скидання (§7) або
використайте інший інтервал. `<id …>` беріть з попередніх відповідей.

## 7. Резервна копія, відновлення, скидання
- Резервна копія: `docker compose exec -T db pg_dump -U roomlab -Fc roomlab > roomlab.dump`. Файл не комітимо.
- Відновлення виконують, коли до БД ніхто не пише:
  1. `docker compose stop web` — зупинити трафік, щоб запити не потрапили в напівочищену базу;
  2. `docker compose exec -T db pg_restore -U roomlab -d roomlab --clean --if-exists < roomlab.dump`;
  3. `docker compose run --rm web python manage.py migrate --noinput` (одноразовий контейнер, `web` ще зупинений);
  4. звірка `count(*)` (нижче), потім `docker compose start web` і `curl -s http://127.0.0.1:8000/health/`.
  Звірте `count(*)` по `booking_room`, `auth_user`, `booking_booking` з даними до збою
  (ціль із `docs/team.md`: менше 5 хв, рядки незмінні; автоматично не вимірюється).
- Збій `web`: сервіс без стану, тому досить `docker compose restart web`. `/health/` = 503 означає, що недоступна БД:
  перевірте `docker compose ps -a` і `docker compose logs db`, потім `docker compose start db`. Саме такий випадок розібрано в `docs/incident.md`.
- Повне скидання (усі дані втрачаються): `docker compose down -v`, `up -d --build`, `migrate`, `seed_demo`.

## 8. Міграції та відкат: застереження
- 0003 лише розширює схему: три nullable-колонки аудиту, check-обмеження `booking_decision_valid` і
  `booking_decision_audit_complete`, а `booking_no_overlap_active` перевизначено на `status IN ('ACTIVE','PENDING')`.
  Ім'я constraint свідомо не змінювали, бо від нього залежить мапінг на 409.
- `migrate` на даних, де вручну з'явилися перетинні `ACTIVE`/`PENDING`, впаде й відкотиться повністю. Такі записи треба розібрати заздалегідь.
- Перед будь-якою міграцією чи відкатом зробіть `pg_dump` (§7).
- **Відкат лише коду** (образ v1.0 на схемі v2) безпечний: v1 ігнорує nullable-колонки й створює тільки `ACTIVE`.
  Але тоді R101 знову бронюється одразу як `ACTIVE`, а наявні `PENDING` не можна погодити.
- **Відкат схеми** (`migrate booking 0002`) виконується ще **образом v2**, бо файл 0003 потрібен для reverse, але
  **без запущеного `web`**. Інакше код v2 між кроками створюватиме нові `PENDING`, які переживуть відкат без захисту
  від перетинів, а після відкату звертатиметься до видалених колонок аудиту й віддаватиме 500. Порядок:
  1. `docker compose stop web` — до кінця процедури до БД ніхто не пише;
  2. вивантажте аудит, якщо він потрібен: колонки `decided_by`, `decided_at`, `decision` після відкату зникають безповоротно;
  3. переведіть `PENDING` у `CANCELLED`, бо після відкату БД більше не захищає `PENDING` від перетинів:
     `docker compose exec db psql -U roomlab -d roomlab -c "UPDATE booking_booking SET status='CANCELLED' WHERE status='PENDING';"`.
     `REJECTED` можна лишити;
  4. `docker compose run --rm web python manage.py migrate booking 0002` — одноразовий контейнер образу v2;
  5. лише тепер `git checkout v1.0 && docker compose up -d --build web` (`web` уперше після зупинки стартує вже на v1.0),
     далі smoke (§6) без кроків approvals.
- Повторне застосування 0003 після періоду роботи v1 впаде, якщо за цей час з'явилися перетини `PENDING`/`ACTIVE`.
- Критерій відкату (`docs/change.md` §8): червоний тест у `booking/tests/test_legacy.py` або змінений legacy-рядок після міграції.

## 9. Відповідальні за підтримку
| Область | Відповідальний | Підстава |
|---|---|---|
| Пошук, створення, exclusion constraint, правило місткості, міграції v2 | SokAlexxx (розробник A) | `docs/team.md`, ризики 1 і 4 |
| `seed_demo`, мої бронювання/скасування, погодження й аудит, командна документація, секрети | TkachAndrii (розробник B) | `docs/team.md`, ризики 2, 3, 5 |
| Рев'ю PR | інший розробник пари | політика рев'ю в `docs/team.md` |
| Відтворення перевірок і локального демо | Команда супроводу | `README.md`, §5 цього документа |

## 10. Стан передавання й відкриті питання

[v2.0](https://github.com/SokAlexxx/roomlab/releases/tag/v2.0) опубліковано. [Протокол передавання](https://github.com/SokAlexxx/roomlab/releases/download/v2.0/handoff.json) фіксує відновлення за 4,281 с, збіг двох контрольних рядків після міграції та запуск локального демо. Правило реагування на три відповіді HTTP 503 наведено в [інструкції з експлуатації](operations.md).

1. Healthcheck PostgreSQL є в `compose.yaml`. Політика автоматичного перезапуску БД лишається відкритою задачею.
2. Час відновлення зафіксовано під час передавання. Постійного автоматичного вимірювання немає.
3. Поріг місткості `100` задано константою в `booking/rooms.py`, а не в налаштуваннях.
4. Сповіщень про 503 немає. `/health/` перевіряє лише `SELECT 1`.
5. `runserver` годиться лише для локального демо. Будь-яке інше розгортання виходить за межі контракту.
6. Конкурентність перевірено тестами лише для двох паралельних запитів. Навантажувальних вимірювань, крім `bench_search`, немає.

## 11. Посилання на релізи й рев'ю
- Репозиторій: https://github.com/SokAlexxx/roomlab
- Реліз v1.0: https://github.com/SokAlexxx/roomlab/releases/tag/v1.0
- Реліз v2.0: https://github.com/SokAlexxx/roomlab/releases/tag/v2.0
- Зміни після v1.0: https://github.com/SokAlexxx/roomlab/compare/v1.0...v2.0
- CI: https://github.com/SokAlexxx/roomlab/actions
- PR (рев'ю):
  | PR | Задача |
  |---|---|
  | [#1](https://github.com/SokAlexxx/roomlab/pull/1) | KAN-11 B1 акаунти, вимоги, ризики |
  | [#2](https://github.com/SokAlexxx/roomlab/pull/2) | KAN-4 A1 пошук |
  | [#3](https://github.com/SokAlexxx/roomlab/pull/3) | KAN-12 B2 мої записи й скасування |
  | [#4](https://github.com/SokAlexxx/roomlab/pull/4) | KAN-5 A2 створення й конкурентність |
  | [#5](https://github.com/SokAlexxx/roomlab/pull/5) | KAN-13 B3 спостереження, інтеграційні тести |
  | [#6](https://github.com/SokAlexxx/roomlab/pull/6) | KAN-6 A3 продуктивність, документація |
  | [#7](https://github.com/SokAlexxx/roomlab/pull/7) | KAN-14 B4 запит на зміну, характеристичні тести |
  | [#8](https://github.com/SokAlexxx/roomlab/pull/8) | KAN-16 B6 інцидент |
  | [#9](https://github.com/SokAlexxx/roomlab/pull/9) | KAN-7 A4 рефакторинг |
  | [#10](https://github.com/SokAlexxx/roomlab/pull/10) | KAN-8 A5 схема й міграція |
  | [#11](https://github.com/SokAlexxx/roomlab/pull/11) | KAN-15 B5 погодження, аудит |
  | [#12](https://github.com/SokAlexxx/roomlab/pull/12) | KAN-9 A6 початковий стан PENDING |
  | [#13](https://github.com/SokAlexxx/roomlab/pull/13) | KAN-10 A7 передавання й інструкція запуску |
- Jira: https://grouplabor.atlassian.net/browse/KAN-10

## 12. Сценарій презентації
Для демонстрації розгорніть локальний Docker-стек v2.0 за §4.
1. Контекст і межі (1 хв): `docs/contract.md`, схема з `docs/architecture.md`.
2. Запуск: `docker compose ps`, `/health/` → 200.
3. Сценарій v1 на R100 (alice/bob): пошук → бронювання 201 `ACTIVE` → конфлікт 409 → суміжний інтервал 201 → чуже скасування 403 → своє скасування 200 → слот знову вільний.
4. Сценарій v2 на R101: bob → 201 `PENDING`, кімната зникає з пошуку → alice отримує 403 на `/api/approvals/` → admin бачить запис → `approve` → 200 `ACTIVE` з аудитом → повтор `approve` повертає 200 з тим самим аудитом → `reject` дає 409.
5. Цілісність: показати exclusion constraint (ADR-1, `docs/migration.md`) і тест `ConcurrentCreate`/`ConcurrentPending` як доказ «рівно один 201».
6. Експлуатація: `pg_dump` → `pg_restore` (§7), розбір інциденту `docs/incident.md` і відкриті питання §10.
7. Підсумок: де брати передавання (цей документ) і хто за що відповідає (§9).
