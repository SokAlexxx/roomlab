"""Room availability search (KAN-4 / A1).

Intervals are half-open [start, end): two intervals overlap when
existing.start < requested.end and existing.end > requested.start,
so a booking ending exactly when another begins is not a conflict.
"""
from datetime import datetime

from django.http import JsonResponse

from booking.http import endpoint, error
from booking.models import Booking, Room

# Statuses that hold a slot. PENDING reserves the slot in v2; CANCELLED and
# REJECTED free it immediately.
BLOCKING_STATUSES = (Booking.Status.ACTIVE, Booking.Status.PENDING)


def parse_instant(value):
    """Parse an ISO 8601 timestamp that carries an explicit offset.

    Returns an aware datetime, or None for missing, malformed or naive input.
    Note: in a query string "+" must be sent as %2B; a raw "+" decodes to a
    space and the value is rejected instead of being guessed.
    """
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def parse_interval(start_raw, end_raw):
    """Return (start, end) for a valid half-open interval, or None."""
    start = parse_instant(start_raw)
    end = parse_instant(end_raw)
    if start is None or end is None or start >= end:
        return None
    return start, end


def overlapping(start, end):
    """Bookings that hold any part of [start, end)."""
    return Booking.objects.filter(status__in=BLOCKING_STATUSES, start__lt=end, end__gt=start)


@endpoint("GET")
def available(request):
    interval = parse_interval(request.GET.get("start"), request.GET.get("end"))
    if interval is None:
        return error("invalid_input", 400)
    busy = overlapping(*interval).values("room_id")
    rooms = Room.objects.exclude(pk__in=busy).order_by("id")
    return JsonResponse([{"id": r.pk, "name": r.name, "capacity": r.capacity} for r in rooms], safe=False)
