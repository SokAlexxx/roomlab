"""Search latency benchmark (KAN-6 / A3). Run only against a disposable database.

Seeds 1000 non-overlapping ACTIVE bookings in dedicated BENCH-* rooms owned by a
dedicated "bench" user (repeat-safe: only missing rows are inserted), then times
authenticated GET /api/rooms/available/ calls through the Django test client.
"""
import base64
import json
import math
import secrets
import time
from datetime import datetime, timedelta, timezone

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.test import Client

from booking.models import Booking, Room

BENCH_PREFIX = "BENCH-"
BENCH_USER = "bench"
ROOM_COUNT = 10
SLOTS_PER_ROOM = 100  # ROOM_COUNT * SLOTS_PER_ROOM = 1000 bookings
ROWS = ROOM_COUNT * SLOTS_PER_ROOM
# v2 sends capacity > 100 to PENDING; bench rooms must stay well below that (checked in seed()).
BENCH_CAPACITY = 10
# Far-future fixed base so bench slots never collide with demo dates.
BASE = datetime(2040, 1, 1, tzinfo=timezone.utc)
SLOT = timedelta(hours=1)
WARMUPS = 10
SAMPLES = 100
P95_LIMIT_MS = 1000.0
PATH = "/api/rooms/available/"


def p95(values):
    """Nearest-rank 95th percentile."""
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def slot(index):
    start = BASE + index * SLOT
    return start, start + SLOT


def request_host():
    """A host the running settings accept; outside the test runner "testserver" is not implied."""
    for host in settings.ALLOWED_HOSTS:
        if host and host != "*" and not host.startswith("."):
            return host
    return "localhost"


class Command(BaseCommand):
    help = ("Benchmark room search on a DISPOSABLE database: seeds BENCH-* rooms, user 'bench' and "
            "1000 bookings; refuses if the database holds any non-bench booking.")

    def add_arguments(self, parser):
        parser.add_argument("--disposable-db", metavar="NAME",
                            help="Optional extra guard: must equal the current database name.")

    def handle(self, *args, **options):
        actual = connection.settings_dict["NAME"]
        expected = options.get("disposable_db")
        if expected is not None and expected != actual:
            raise CommandError(f"refusing to run: --disposable-db must equal the current database name ({actual!r})")

        password = secrets.token_urlsafe(24)
        rooms = self.seed(password)
        bench_ids = {room.pk for room in rooms}
        rows = Booking.objects.filter(room__in=rooms, status=Booking.Status.ACTIVE).count()

        auth = "Basic " + base64.b64encode(f"{BENCH_USER}:{password}".encode()).decode()
        client = Client(HTTP_HOST=request_host(), HTTP_AUTHORIZATION=auth)
        errors = 0
        timings = []
        for i in range(WARMUPS + SAMPLES):
            start, end = slot(i % SLOTS_PER_ROOM)
            params = {"start": start.isoformat(), "end": end.isoformat()}
            began = time.perf_counter()
            response = client.get(PATH, params)
            elapsed_ms = (time.perf_counter() - began) * 1000
            ok = response.status_code == 200
            # Every bench room is booked in every seeded slot, so none may be listed.
            if ok and bench_ids & {room["id"] for room in response.json()}:
                ok = False
            if i >= WARMUPS:
                timings.append(elapsed_ms)
            errors += not ok

        result = {"rows": rows, "samples": len(timings), "p95_ms": round(p95(timings), 2), "errors": errors}
        self.stdout.write(json.dumps(result))
        if rows != ROWS:
            raise CommandError(f"expected {ROWS} bench bookings, found {rows}")
        if errors:
            raise CommandError(f"{errors} request(s) failed (non-200 or wrong result)")
        if result["p95_ms"] > P95_LIMIT_MS:
            raise CommandError(f"p95 {result['p95_ms']} ms exceeds {P95_LIMIT_MS:.0f} ms")

    def seed(self, password):
        """Create bench user, rooms and missing bookings; all guards run inside the same transaction."""
        User = get_user_model()
        with transaction.atomic():
            user, _ = User.objects.select_for_update().get_or_create(username=BENCH_USER)
            if user.is_staff or user.is_superuser:
                raise CommandError(f"refusing to run: user {BENCH_USER!r} is privileged, not a bench account")
            rooms = []
            for n in range(1, ROOM_COUNT + 1):
                room, _ = Room.objects.select_for_update().get_or_create(
                    name=f"{BENCH_PREFIX}{n:02d}", defaults={"capacity": BENCH_CAPACITY})
                if room.capacity != BENCH_CAPACITY:
                    # An existing BENCH-* room with another capacity (e.g. >100, the v2 approval
                    # path) is not ours: inserting ACTIVE rows there would bypass PENDING.
                    raise CommandError(f"refusing to run: room {room.name!r} has capacity {room.capacity}, "
                                       f"expected {BENCH_CAPACITY}")
                rooms.append(room)
            # Disposable-database guard: a fresh stack (migrate + optional seed_demo) has no
            # bookings except earlier bench runs. Any other booking means real/demo usage.
            foreign = Booking.objects.exclude(owner=user, room__in=rooms).count()
            if foreign:
                raise CommandError(f"refusing to run: {foreign} non-bench booking(s) found; "
                                   "bench_search runs only on a disposable database")
            user.is_active = True
            user.set_password(password)
            user.save()
            existing = set(Booking.objects.filter(room__in=rooms, owner=user, status=Booking.Status.ACTIVE)
                           .values_list("room_id", "start"))
            missing = []
            for room in rooms:
                for index in range(SLOTS_PER_ROOM):
                    start, end = slot(index)
                    if (room.pk, start) not in existing:
                        missing.append(Booking(room=room, owner=user, start=start, end=end,
                                               status=Booking.Status.ACTIVE))
            # The exclusion constraint still guards against any overlap with other rows.
            Booking.objects.bulk_create(missing)
        return rooms
