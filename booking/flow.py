"""Own bookings and cancellation (KAN-12 / B2).

The owner is always request.user assigned by booking.http.endpoint; clients
never send an owner id. Cancellation locks the row with SELECT ... FOR UPDATE
inside a transaction, so concurrent cancels (or a later v2 approval decision)
see the committed status and cannot both succeed.
"""
import json

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone

from booking.http import endpoint, error, record
from booking.models import Booking

# Statuses an owner may cancel. PENDING is cancellable per the v2 contract.
CANCELLABLE = (Booking.Status.ACTIVE, Booking.Status.PENDING)


@endpoint("GET")
def mine(request):
    rows = Booking.objects.filter(owner=request.user).order_by("start", "id")
    return JsonResponse([record(row) for row in rows], safe=False)


@endpoint("POST")
def cancel(request, booking_id):
    with transaction.atomic():
        row = Booking.objects.select_for_update().filter(pk=booking_id).first()
        if row is None:
            return error("not_found", 404)
        if row.owner_id != request.user.pk:
            return error("forbidden", 403)
        if row.status not in CANCELLABLE:
            return error("invalid_transition", 409)
        row.status = Booking.Status.CANCELLED
        row.save(update_fields=["status"])
    return JsonResponse(record(row))


# --- v2: черга та рішення staff (KAN-15 / B5) ---------------------------------
# Рішення -> новий статус. Відхилення звільняє слот: виключення діє лише для ACTIVE/PENDING.
OUTCOME = {
    Booking.Decision.APPROVE: Booking.Status.ACTIVE,
    Booking.Decision.REJECT: Booking.Status.REJECTED,
}


def decision_record(row):
    data = record(row)
    data.update(decided_by=row.decided_by_id, decided_at=row.decided_at.isoformat(), decision=row.decision)
    return data


@endpoint("GET")
def approvals(request):
    if not request.user.is_staff:
        return error("forbidden", 403)
    rows = Booking.objects.filter(status=Booking.Status.PENDING).order_by("start", "id")
    return JsonResponse([record(row) for row in rows], safe=False)


@endpoint("POST")
def decide(request, booking_id):
    if not request.user.is_staff:
        return error("forbidden", 403)
    try:
        body = json.loads(request.body or b"{}")
    except ValueError:
        return error("invalid_input", 400)
    decision = body.get("decision") if isinstance(body, dict) else None
    if not isinstance(decision, str) or decision not in OUTCOME:
        return error("invalid_input", 400)
    # Блокування рядка серіалізує рішення і скасування: другий запит чекає коміту першого
    # і бачить уже остаточний статус та аудит.
    with transaction.atomic():
        row = Booking.objects.select_for_update().filter(pk=booking_id).first()
        if row is None:
            return error("not_found", 404)
        if row.status == Booking.Status.PENDING:
            row.status = OUTCOME[decision]
            row.decision = decision
            row.decided_by = request.user
            row.decided_at = timezone.now()
            # Статус і аудит (хто, коли, що) пишуться одним UPDATE в одній транзакції.
            row.save(update_fields=["status", "decision", "decided_by", "decided_at"])
        elif row.decision == decision and row.status == OUTCOME[decision]:
            pass  # той самий повтор: початковий аудит без змін
        else:
            return error("invalid_transition", 409)
    return JsonResponse(decision_record(row))
