from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import DateTimeRangeField, RangeBoundary, RangeOperators
from django.db import models

# Name of the PostgreSQL exclusion constraint; the API maps only this one to 409.
OVERLAP_CONSTRAINT = "booking_no_overlap_active"


class TsTzRange(models.Func):
    """tstzrange(start, end, '[)'): the half-open interval used by the exclusion constraint."""
    function = "TSTZRANGE"
    output_field = DateTimeRangeField()


class Room(models.Model):
    name = models.CharField(max_length=40, unique=True)
    capacity = models.PositiveIntegerField()


class Booking(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "ACTIVE"
        CANCELLED = "CANCELLED"
        # Reserved for the v2.0 approval flow; v1.0 never creates them.
        PENDING = "PENDING"
        REJECTED = "REJECTED"

    room = models.ForeignKey(Room, on_delete=models.PROTECT)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    start = models.DateTimeField()
    end = models.DateTimeField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.ACTIVE)

    class Decision(models.TextChoices):
        APPROVE = "approve"
        REJECT = "reject"

    # v2 approval audit (KAN-8 / A5). Schema only: nothing writes these yet.
    # NULL for every legacy row and for bookings that were never decided.
    decided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="booking_decisions")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision = models.CharField(max_length=8, choices=Decision.choices, null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(start__lt=models.F("end")),
                                   name="booking_start_before_end"),
            models.CheckConstraint(condition=models.Q(status__in=["ACTIVE", "CANCELLED", "PENDING", "REJECTED"]),
                                   name="booking_status_valid"),
            # Audit is all-or-nothing: actor, time and outcome are stored together or not at all.
            models.CheckConstraint(condition=models.Q(decision__isnull=True) | models.Q(decision__in=["approve", "reject"]),
                                   name="booking_decision_valid"),
            models.CheckConstraint(
                condition=(models.Q(decided_by__isnull=True, decided_at__isnull=True, decision__isnull=True)
                           | models.Q(decided_by__isnull=False, decided_at__isnull=False, decision__isnull=False)),
                name="booking_decision_audit_complete"),
            # Same room and overlapping [start, end) cannot both hold the slot (ACTIVE or PENDING).
            # Name kept from v1: the API maps exactly this name to 409 (room "=" needs btree_gist).
            ExclusionConstraint(
                name=OVERLAP_CONSTRAINT,
                expressions=[
                    ("room", RangeOperators.EQUAL),
                    (TsTzRange("start", "end", RangeBoundary()), RangeOperators.OVERLAPS),
                ],
                condition=models.Q(status__in=["ACTIVE", "PENDING"]),
            ),
        ]
