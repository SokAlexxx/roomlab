# Рішення staff і переходи станів (KAN-15 / B5)

## Стани бронювання
`PENDING` (очікує) · `ACTIVE` · `REJECTED` · `CANCELLED`. `PENDING` і `ACTIVE` займають слот (exclusion constraint); `REJECTED` і `CANCELLED` слот звільняють.

## Переходи
| Звідки | Дія | Куди | Аудит (`decided_by`, `decided_at`, `decision`) |
|---|---|---|---|
| PENDING | staff `approve` | ACTIVE | записується разом зі статусом |
| PENDING | staff `reject` | REJECTED | записується разом зі статусом, слот вільний |
| PENDING | власник `cancel` | CANCELLED | не записується (рішення не було) |
| ACTIVE | власник `cancel` | CANCELLED | якщо було схвалення — зберігається |
| ACTIVE (схвалене) | повтор `approve` | без змін, 200 | початковий (той самий актор і час) |
| REJECTED | повтор `reject` | без змін, 200 | початковий |
| ACTIVE (схвалене) | `reject` / REJECTED | `approve` | 409 `invalid_transition`, аудит не змінюється |
| CANCELLED, legacy ACTIVE без рішення | будь-яке рішення | 409 `invalid_transition` | не змінюється (немає ретроактивного погодження) |
| REJECTED, CANCELLED | `cancel` | 409 `invalid_transition` | — |

## API
- `GET /api/approvals/` — лише staff (інші 403, без автентифікації 401), масив `PENDING`, порядок `start, id`.
- `POST /api/approvals/{id}/decision/` `{"decision":"approve"|"reject"}` — лише staff; невалідне тіло/рішення → 400 `invalid_input`; немає запису → 404. Відповідь 200: запис + `decided_by` (id користувача), `decided_at`, `decision`.
- Повтор того самого рішення (навіть іншим staff) повертає початковий аудит; повтор не оновлює час.

## Конкурентність
Рішення і скасування виконуються в `transaction.atomic()` з `SELECT ... FOR UPDATE` по рядку бронювання. Статус і аудит пишуться одним `UPDATE`. Другий запит чекає коміту першого, перечитує вже остаточний стан і відповідає за таблицею вище: протилежне рішення — 409, те саме — 200 з початковим аудитом. Перевірка стану робиться лише після блокування. Ранні відповіді (403/400/404) не змінюють даних. Check-обмеження `booking_decision_audit_complete` не дозволяє частковий аудит.
