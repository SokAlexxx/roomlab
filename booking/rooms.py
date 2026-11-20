"""Room availability search (KAN-4 / A1).

Intervals are half-open [start, end): two intervals overlap when
existing.start < requested.end and existing.end > requested.start,
so a booking ending exactly when another begins is not a conflict.
"""
import json
from datetime import datetime

from django.db import IntegrityError, transaction
from django.http import JsonResponse

from booking.http import endpoint, error, record
from booking.models import OVERLAP_CONSTRAINT, Booking, Room

# Statuses that hold a slot. PENDING reserves the slot in v2; CANCELLED and
# REJECTED free it immediately.
BLOCKING_STATUSES = (Booking.Status.ACTIVE, Booking.Status.PENDING)

# v2 (KAN-9 / A6): rooms larger than this need staff approval, so new bookings
# start PENDING; rooms up to and including it stay auto-ACTIVE as in v1.
APPROVAL_CAPACITY_THRESHOLD = 100


def initial_status(room):
    """Status for a new booking: PENDING when capacity > 100, otherwise ACTIVE.

    Evaluated only at creation time; existing rows are never re-evaluated, so a
    legacy ACTIVE booking on a large room keeps its status (no retroactive approval).
    """
    if room.capacity > APPROVAL_CAPACITY_THRESHOLD:
        return Booking.Status.PENDING
    return Booking.Status.ACTIVE


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


def is_overlap_violation(exc):
    """True only for the named exclusion constraint, never for other integrity errors."""
    diag = getattr(exc.__cause__, "diag", None)
    return getattr(diag, "constraint_name", None) == OVERLAP_CONSTRAINT


def parse_booking_payload(raw_body):
    """Validate a create request body (KAN-7 / A4: extracted from create).

    Returns (room_id, start, end) or None for any invalid input. No database
    access, so it is pure and unit-testable on its own.
    """
    try:
        body = json.loads(raw_body)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    room_id = body.get("room_id")
    if not isinstance(room_id, int) or isinstance(room_id, bool) or room_id <= 0:
        return None
    # start < end is checked here so the DB check constraint is never the user-facing error.
    interval = parse_interval(body.get("start"), body.get("end"))
    if interval is None:
        return None
    return room_id, interval[0], interval[1]


def reserve(owner, room_id, start, end):
    """Insert a booking for [start, end) (KAN-7 / A4: extracted from create).

    Returns the new Booking, or None when the room does not exist. An overlap
    surfaces as IntegrityError raised after the atomic block has rolled back.
    v2: the status comes from initial_status(room). ACTIVE and PENDING are both
    covered by the exclusion constraint, so either one holds the slot.
    """
    with transaction.atomic():
        # Locking the room row serialises concurrent inserts for one room. Without it two
        # conflicting INSERTs can each wait on the other's exclusion check and deadlock;
        # with it the later INSERT sees the committed row and fails on the named constraint.
        room = Room.objects.select_for_update().filter(pk=room_id).first()
        if room is None:
            return None
        return Booking.objects.create(room=room, owner=owner, start=start, end=end,
                                      status=initial_status(room))


@endpoint("POST")
def create(request):
    """POST /api/bookings/ (KAN-5 / A2; v2 status by capacity, KAN-9 / A6).

    Capacity > 100 -> PENDING, otherwise ACTIVE; both reserve the slot.

    The application does no read-then-write availability check: PostgreSQL's
    exclusion constraint is the single source of truth, so two concurrent
    requests cannot both succeed. This view only maps outcomes to HTTP.
    """
    payload = parse_booking_payload(request.body)
    if payload is None:
        return error("invalid_input", 400)
    try:
        row = reserve(request.user, *payload)
    except IntegrityError as exc:
        if is_overlap_violation(exc):
            return error("slot_conflict", 409)
        raise
    if row is None:
        return error("not_found", 404)
    return JsonResponse(record(row), status=201)
