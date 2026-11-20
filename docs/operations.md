# Експлуатація RoomLab (локальне демо)

## Запуск і перевірка
- Повну перевірку (Docker build, Ruff, `manage.py check`, `makemigrations --check`, `migrate`, повний набір тестів на PostgreSQL в одноразовому стеку) запускає вбудований операційний раннер: з кореня репозиторію `py -3.12 .roomlab-check.py`. Його ж повторює контролер RoomLab перед релізом. Docker, релізи та фінальне демо веде контролер.
- Ручний запуск: `PG_PASS=… DEMO_PASS=… DJANGO_SECRET_KEY=… docker compose up -d --build`, потім `docker compose exec web python manage.py migrate` і `docker compose exec web python manage.py seed_demo`.

## Цілісність даних
- Джерело істини — PostgreSQL. Check constraint `booking_start_before_end`, `booking_status_valid` і exclusion constraint `booking_no_overlap_active` (GiST, `btree_gist`, `[start,end)`) не можна вимикати чи обходити прямими SQL-вставками.
- Схему змінюють лише міграціями; перед релізом `makemigrations --check` має бути чистим (це робить раннер).
- `seed_demo` ідемпотентний: кімнати шукаються за унікальною назвою, наявні бронювання не змінюються, паролі демо-користувачів скидаються з `DEMO_PASS`.
- `Room`/`User` захищені `on_delete=PROTECT`: кімнату чи користувача з бронюваннями видалити не можна — історія лишається.

## Бенчмарк пошуку (`bench_search`)
- Тільки в **одноразовій** базі, ніколи в демо-базі з даними. Окремий стек з власним томом:
  ```
  WEB_PORT=8001 docker compose -p roomlab-bench up -d --build
  docker compose -p roomlab-bench exec web python manage.py migrate
  docker compose -p roomlab-bench exec web python manage.py seed_demo   # необов'язково
  docker compose -p roomlab-bench exec web python manage.py bench_search
  docker compose -p roomlab-bench down -v
  ```
- Контролер (раннер `.roomlab-check.py` для A3 і релізний стенд) запускає `seed_demo`, а потім `bench_search` без аргументів в одноразовому стеку, який одразу видаляє.
- Захист (усі перевірки виконуються в одній транзакції з сідом, після `select_for_update`): команда пише лише в кімнати `BENCH-*` від імені користувача `bench` і нічого не змінює й не видаляє в інших даних. Команда відмовляє в запуску, якщо:
  - у базі є **будь-яке** бронювання, крім bench-бронювань у `BENCH-*`. Це ознака робочої чи демо-бази, а не одноразової: свіжий стек після `migrate` + `seed_demo` бронювань не має;
  - наявна кімната `BENCH-*` має місткість ≠ 10, бо інакше вставка `ACTIVE` обійшла б v2-правило «місткість >100 → `PENDING`»;
  - `bench` є staff/superuser.
  Необов'язковий `--disposable-db NAME` додатково вимагає, щоб назва збігалася з назвою поточної БД. Запити надсилаються з `Host` зі списку `ALLOWED_HOSTS`, тож команда працює і поза тест-раннером.
- Команда створює користувача `bench` (не staff, новий випадковий пароль на кожен запуск), кімнати `BENCH-01…BENCH-10` і 1000 неперетинних `ACTIVE` бронювань (2040-01-01, слоти по 1 год). Повторний запуск додає лише відсутні рядки.
- Виконує 10 прогрівальних і 100 вимірюваних автентифікованих `GET /api/rooms/available/` через Django test client. Друкує JSON `{"rows", "samples", "p95_ms", "errors"}`; код виходу ≠ 0, якщо будь-яка відповідь не 200 (або містить зайняту bench-кімнату), `rows ≠ 1000` чи `p95_ms > 1000`.

## Спостережуваність
- `GET /health/` (без автентифікації): `200 {"status":200,"elapsed_ms":…}` якщо `SELECT 1` до БД успішний, інакше `503`. Підходить для перевірки живості.
- Логи: `docker compose logs -f web` (runserver пише кожен запит зі статусом), `docker compose logs db` (помилки PostgreSQL, зокрема порушення constraint).
- Помилки API — JSON `{"code","detail"}`; часті `409 slot_conflict` — нормальна конкуренція, `500` — дефект.
- Затримку пошуку вимірюйте `bench_search` (p95). Автентифікація Basic кешується на 60 с, тож вимір відображає API, а не PBKDF2.

## Відновлення
- Резервна копія: `docker compose exec -T db pg_dump -U roomlab -Fc roomlab > roomlab.dump`.
- Відновлення: `docker compose exec -T db pg_restore -U roomlab -d roomlab --clean --if-exists < roomlab.dump`, потім `migrate` і перевірка `/health/`.
- Сервіс `web` без стану: при збої — `docker compose restart web`. Якщо БД недоступна, `/health/` дає 503; перевірте `docker compose ps` і `logs db`.
- Повне скидання демо (втрата даних): `docker compose down -v`, `up -d`, `migrate`, `seed_demo`.
- Bench-дані видаляються разом з одноразовим стеком (`down -v`); у демо-базі їх не повинно бути. Якщо `bench_search` все ж запустили в демо-базі без бронювань, кімнати `BENCH-*` з'являться в пошуку alice і bob. У такому разі відновіть базу з дампу або повністю скиньте демо.
