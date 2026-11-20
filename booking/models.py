from django.conf import settings
from django.db import models


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

    class Meta:
        constraints = [
            models.CheckConstraint(condition=models.Q(start__lt=models.F("end")),
                                   name="booking_start_before_end"),
            models.CheckConstraint(condition=models.Q(status__in=["ACTIVE", "CANCELLED", "PENDING", "REJECTED"]),
                                   name="booking_status_valid"),
        ]
