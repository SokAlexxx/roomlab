# RoomLab

RoomLab — локальний API пошуку й бронювання університетських аудиторій на Django та PostgreSQL. У v2.0 додано погодження бронювань аудиторій місткістю понад 100 осіб і аудит рішень адміністратора.

## Матеріали проєкту

- [Підсумковий звіт і навігатор](https://grouplabor.atlassian.net/wiki/spaces/RoomLab/pages/622643)
- [Дошка Jira KAN](https://grouplabor.atlassian.net/jira/software/projects/KAN/boards/1)
- [Реліз v1.0](https://github.com/SokAlexxx/roomlab/releases/tag/v1.0) · [Реліз v2.0](https://github.com/SokAlexxx/roomlab/releases/tag/v2.0)
- [Контракт API](docs/contract.md) · [Інструкція передавання](docs/handoff.md)

## Локальне демо

Розпакуйте `roomlab-source.zip` із релізу v2.0. У корені архіву відкрийте Git Bash або іншу POSIX-оболонку й виконайте:

```bash
export PG_PASS='roomlab-local-db' DEMO_PASS='roomlab-local-demo'
export DJANGO_SECRET_KEY='roomlab-local-development-only'
docker compose up -d --build
docker compose exec web python manage.py migrate --noinput
docker compose exec web python manage.py seed_demo
curl -fsS http://127.0.0.1:8000/health/
```

Очікується HTTP 200. Наведені значення призначені для локального демо. Порт web публікується лише на `127.0.0.1`. Приклади запитів і порядок відкату наведено в [інструкції передавання](docs/handoff.md).

## Перевірки

Виконайте [команди CI для Docker Compose](docs/handoff.md#5-тестування). Перевірка охоплює Ruff, узгодженість міграцій і тести Django на PostgreSQL. [Результат CI релізу v2.0](https://github.com/SokAlexxx/roomlab/actions/runs/44770076147)
