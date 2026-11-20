# Передавання RoomLab (KAN-10 / A7)

Документ для того, хто підтримуватиме RoomLab після команди. Усе нижче взято з коду репозиторію
(`compose.yaml`, `Dockerfile`, `config/`, `booking/`, `.github/workflows/ci.yml`) і наявних документів
`docs/*.md`. Поведінку API визначає `docs/contract.md`. Тут немає результатів прогонів чи ретроспектив,
яких не було: дані про виконані перевірки є лише в `evidence/*.md`.

Обсяг: лише локальне демо. Немає реєстрації, веб-інтерфейсу, e-mail, зовнішнього хостингу чи реальних даних.

## 1. Склад системи
| Частина | Де | Примітка |
|---|---|---|
| API (Django, `runserver` на 0.0.0.0:8000 у контейнері) | `Dockerfile`, `config/`, `booking/` | Сервер розробки, тільки для локального демо. Порт публікується лише на `127.0.0.1:${WEB_PORT:-8000}` |
| PostgreSQL 16 | сервіс `db` у `compose.yaml`, том `data` | Джерело істини. Healthcheck `pg_isready`, від якого залежить `web` |
| Маршрути | `config/urls.py`, `booking/rooms_urls.py`, `booking/flow_urls.py` | `/health/`, `/api/rooms/available/`, `/api/bookings/`, `/api/bookings/mine/`, `/api/bookings/{id}/cancel/`, `/api/approvals/`, `/api/approvals/{id}/decision/` |
| Міграції | `booking/migrations/0001…0003` | 0003 — аудит рішень і розширений overlap-constraint (див. §6) |
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
curl -s http://127.0.0.1:8000/health/           # очікується {"status": 200, ...}
```
Конкретний реліз розгортають з тегу (`git checkout v1.0`, далі ті самі кроки). Тег v2.0 ще не створено (§9).

## 5. Тестування
- **Повна перевірка (основна):** `py -3.12 .roomlab-check.py` з кореня. Вона збирає образ, запускає Ruff, `manage.py check`,
  `makemigrations --check`, `migrate` і повний набір тестів на PostgreSQL в одноразовому стеку. Успіх — рядок
  `ROOMLAB_CHECK_OK`. Ту саму перевірку повторює контролер RoomLab.
- **Вручну, так само як у CI:**
  ```
  docker compose build web
  docker compose up -d --wait db
  docker compose run --rm web ruff check .
  docker compose run --rm web python manage.py makemigrations --check --dry-run
  docker compose run --rm web python manage.py migrate --noinput
  docker compose run --rm web python manage.py test
  docker compose down
  ```
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
curl -s -u alice:$DEMO_PASS -X POST $B/api/bookings/<id alice>/cancel/ -d '{}'   # 200 CANCELLED
curl -s -u alice:$DEMO_PASS $B/api/bookings/mine/                      # власні записи, зокрема CANCELLED
```
Smoke додає в демо-базу записи на 2030-01-01. Для чистого демо після нього виконайте скидання (§7) або
використайте інший інтервал. `<id …>` беріть з попередніх відповідей.

## 7. Резервна копія, відновлення, скидання
- Резервна копія: `docker compose exec -T db pg_dump -U roomlab -Fc roomlab > roomlab.dump`. Файл не комітимо.
- Відновлення: `docker compose exec -T db pg_restore -U roomlab -d roomlab --clean --if-exists < roomlab.dump`,
  потім `docker compose exec web python manage.py migrate --noinput` і `curl -s http://127.0.0.1:8000/health/`.
  Після цього звірте `count(*)` по `booking_room`, `auth_user`, `booking_booking` з даними до збою
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
- **Відкат схеми** (`migrate booking 0002`) виконується ще **на образі v2**, бо файл 0003 потрібен для reverse. Лише після цього розгортають v1.0:
  1. вивантажте аудит, якщо він потрібен: колонки `decided_by`, `decided_at`, `decision` після відкату зникають безповоротно;
  2. переведіть `PENDING` у `CANCELLED`, бо після відкату БД більше не захищає `PENDING` від перетинів:
     `docker compose exec db psql -U roomlab -d roomlab -c "UPDATE booking_booking SET status='CANCELLED' WHERE status='PENDING';"`.
     `REJECTED` можна лишити;
  3. `docker compose exec web python manage.py migrate booking 0002`;
  4. `git checkout v1.0 && docker compose up -d --build web`, далі smoke (§6) без кроків approvals.
- Повторне застосування 0003 після періоду роботи v1 впаде, якщо за цей час з'явилися перетини `PENDING`/`ACTIVE`.
- Критерій відкату (`docs/change.md` §8): червоний тест у `booking/tests/test_legacy.py` або змінений legacy-рядок після міграції.

## 9. Відповідальні за підтримку
| Область | Відповідальний | Підстава |
|---|---|---|
| Пошук, створення, exclusion constraint, правило місткості, міграції v2 | SokAlexxx (розробник A) | `docs/team.md`, ризики 1 і 4 |
| `seed_demo`, мої бронювання/скасування, погодження й аудит, командна документація, секрети | TkachAndrii (розробник B) | `docs/team.md`, ризики 2, 3, 5 |
| Рев'ю PR | інший розробник пари | політика рев'ю в `docs/team.md` |
| Git, Jira, CI, релізи, фінальне демо, повтор `.roomlab-check.py` | контролер RoomLab (автоматизація) | `README.md`, `docs/operations.md` |

## 10. Відкриті питання
1. **Реліз v2.0 (REL2) ще не створено.** У репозиторії є лише тег `v1.0`. Тег і реліз v2.0 створює контролер.
2. **Коригувальна задача 1 з інциденту виконана лише частково.** Healthcheck `db` у `compose.yaml` є, а `restart: unless-stopped`
   немає, тож зупинена БД сама не піднімається. Власник SokAlexxx (`docs/incident.md`).
3. **Коригувальна задача 2 з інциденту не виконана.** У `docs/operations.md` є лише загальна порада (`ps`, `logs db`),
   а правила ескалації з порогом «≥3 поспіль 503 → `ps`, `logs db`, старт `db`» немає. Власник TkachAndrii. Тимчасово можна користуватися §7 цього документа.
4. Відновлення з дампу менше ніж за 5 хв не вимірюється автоматично (`docs/quality.md`).
5. Поріг місткості `100` задано константою в `booking/rooms.py`, а не в налаштуваннях.
6. Сповіщень про 503 немає. `/health/` перевіряє лише `SELECT 1`.
7. `runserver` годиться лише для локального демо. Будь-яке інше розгортання виходить за межі контракту.
8. Конкурентність перевірено тестами лише для двох паралельних запитів. Навантажувальних вимірювань, крім `bench_search`, немає.

## 11. Посилання на релізи й рев'ю
Посилання побудовано з `origin` (`https://github.com/SokAlexxx/roomlab-agents`) і номерів PR у повідомленнях комітів `main`.
- Репозиторій: https://github.com/SokAlexxx/roomlab-agents
- Реліз v1.0: https://github.com/SokAlexxx/roomlab-agents/releases/tag/v1.0
- Реліз v2.0 (запланований, ще не існує): https://github.com/SokAlexxx/roomlab-agents/releases/tag/v2.0
- Зміни після v1.0: https://github.com/SokAlexxx/roomlab-agents/compare/v1.0...main
- CI: https://github.com/SokAlexxx/roomlab-agents/actions
- PR (рев'ю):
  | PR | Задача |
  |---|---|
  | [#1](https://github.com/SokAlexxx/roomlab-agents/pull/1) | KAN-11 B1 акаунти, вимоги, ризики |
  | [#2](https://github.com/SokAlexxx/roomlab-agents/pull/2) | KAN-4 A1 пошук |
  | [#3](https://github.com/SokAlexxx/roomlab-agents/pull/3) | KAN-12 B2 мої записи й скасування |
  | [#4](https://github.com/SokAlexxx/roomlab-agents/pull/4) | KAN-5 A2 створення й конкурентність |
  | [#5](https://github.com/SokAlexxx/roomlab-agents/pull/5) | KAN-13 B3 спостереження, інтеграційні тести |
  | [#6](https://github.com/SokAlexxx/roomlab-agents/pull/6) | KAN-6 A3 продуктивність, документація |
  | [#7](https://github.com/SokAlexxx/roomlab-agents/pull/7) | KAN-14 B4 запит на зміну, характеристичні тести |
  | [#8](https://github.com/SokAlexxx/roomlab-agents/pull/8) | KAN-16 B6 інцидент |
  | [#9](https://github.com/SokAlexxx/roomlab-agents/pull/9) | KAN-7 A4 рефакторинг |
  | [#10](https://github.com/SokAlexxx/roomlab-agents/pull/10) | KAN-8 A5 схема й міграція |
  | [#11](https://github.com/SokAlexxx/roomlab-agents/pull/11) | KAN-15 B5 погодження, аудит |
  | [#12](https://github.com/SokAlexxx/roomlab-agents/pull/12) | KAN-9 A6 початковий стан PENDING |
- PR цієї задачі (KAN-10 / A7) і його рев'ю створює контролер. Номера поки немає.
- Jira: https://grouplabor.atlassian.net/browse/KAN-10

## 12. Запланований сценарій презентації
Це план, а не запис виконаного демо. Стенд — локальний Docker-стек, розгорнутий за §4 на релізі v2.0, коли той з'явиться.
1. Контекст і межі (1 хв): `docs/contract.md`, схема з `docs/architecture.md`.
2. Запуск: `docker compose ps`, `/health/` → 200.
3. Сценарій v1 на R100 (alice/bob): пошук → бронювання 201 `ACTIVE` → конфлікт 409 → суміжний інтервал 201 → чуже скасування 403 → своє скасування 200 → слот знову вільний.
4. Сценарій v2 на R101: bob → 201 `PENDING`, кімната зникає з пошуку → alice отримує 403 на `/api/approvals/` → admin бачить запис → `approve` → 200 `ACTIVE` з аудитом → повтор `approve` повертає 200 з тим самим аудитом → `reject` дає 409.
5. Цілісність: показати exclusion constraint (ADR-1, `docs/migration.md`) і тест `ConcurrentCreate`/`ConcurrentPending` як доказ «рівно один 201».
6. Експлуатація: `pg_dump` → `pg_restore` (§7), розбір інциденту `docs/incident.md` і відкриті питання §10.
7. Підсумок: де брати передавання (цей документ) і хто за що відповідає (§9).
