"""Own bookings and cancellation (KAN-12 / B2).

The owner is always request.user assigned by booking.http.endpoint; clients
never send an owner id. Cancellation locks the row with SELECT ... FOR UPDATE
inside a transaction, so concurrent cancels (or a later v2 approval decision)
see the committed status and cannot both succeed.
"""
from django.db import transaction
from django.http import JsonResponse

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
