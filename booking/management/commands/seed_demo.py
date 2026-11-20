import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from booking.models import Room

ROOMS = (("R100", 100), ("R101", 101))
# username -> is_staff; admin is the only staff account.
USERS = (("alice", False), ("bob", False), ("admin", True))


class Command(BaseCommand):
    help = "Idempotently create or reset demo rooms and users (password from DEMO_PASS)."

    def handle(self, *args, **options):
        password = os.environ.get("DEMO_PASS", "")
        if not password:
            raise CommandError("DEMO_PASS environment variable must be set and non-empty")
        User = get_user_model()
        created = []
        with transaction.atomic():
            for name, capacity in ROOMS:
                # Lookup by the unique name keeps the existing primary key and bookings.
                room, new = Room.objects.select_for_update().get_or_create(
                    name=name, defaults={"capacity": capacity})
                if new:
                    created.append(name)
                elif room.capacity != capacity:
                    room.capacity = capacity
                    room.save(update_fields=["capacity"])
            for username, is_staff in USERS:
                user, new = User.objects.select_for_update().get_or_create(username=username)
                if new:
                    created.append(username)
                user.is_staff = is_staff
                user.is_superuser = False
                user.is_active = True
                # Always reset, so a rerun restores the documented demo password.
                user.set_password(password)
                user.save()
        self.stdout.write(self.style.SUCCESS(
            "seed_demo ok; created: " + (", ".join(created) if created else "nothing (updated existing)")))
