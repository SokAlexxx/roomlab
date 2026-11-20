# RoomLab
Two-person local room-booking API. See docs/contract.md. The RoomLab controller runs Docker, checks, releases and the final local demo.

## Швидкий старт (локальне демо)
```
export PG_PASS=... DEMO_PASS=... DJANGO_SECRET_KEY=...
docker compose up -d --build
docker compose exec web python manage.py migrate --noinput
docker compose exec web python manage.py seed_demo
curl -s http://127.0.0.1:8000/health/
```
Повна перевірка: `py -3.12 .roomlab-check.py` (очікується `ROOMLAB_CHECK_OK`).

Передавання (розгортання, тести, smoke, відновлення, відкат, відповідальні, відкриті питання): [docs/handoff.md](docs/handoff.md).
