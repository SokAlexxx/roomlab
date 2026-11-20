import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

from django.contrib.auth import get_user_model
from django.db import IntegrityError, connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase

from booking.models import OVERLAP_CONSTRAINT, Booking, Room
from booking.rooms import is_overlap_violation

PATH = "/api/bookings/"
UTC = timezone.utc


def at(hour, minute=0):
    return datetime(2030, 1, 1, hour, minute, tzinfo=UTC)


def auth(name, password="test"):
    return "Basic " + base64.b64encode(f"{name}:{password}".encode()).decode()


class CreateBooking(TestCase):
    def setUp(self):
        self.r100 = Room.objects.create(name="R100", capacity=100)
        self.r101 = Room.objects.create(name="R101", capacity=101)
        self.alice = get_user_model().objects.create_user("alice", password="test")
        self.bob = get_user_model().objects.create_user("bob", password="test")
        self.data = {"room_id": self.r100.pk, "start": "2030-01-01T10:00:00Z", "end": "2030-01-01T11:00:00Z"}

    def post(self, data, user="alice", raw=None):
        body = raw if raw is not None else data
        return self.client.post(PATH, body, content_type="application/json", HTTP_AUTHORIZATION=auth(user))

    def test_runs_on_postgresql(self):
        self.assertEqual(connection.vendor, "postgresql")

    def test_creates_active_booking(self):
        response = self.post(self.data)
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        row = Booking.objects.get()
        self.assertEqual(body, {"id": row.pk, "room_id": self.r100.pk, "start": "2030-01-01T10:00:00+00:00",
                                "end": "2030-01-01T11:00:00+00:00", "status": "ACTIVE"})
        self.assertEqual(row.owner, self.alice)
        self.assertEqual((row.start, row.end), (at(10), at(11)))

    def test_owner_comes_from_server_not_body(self):
        response = self.post(dict(self.data, owner_id=self.bob.pk))
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Booking.objects.get().owner, self.alice)

    def test_requires_authentication(self):
        response = self.client.post(PATH, self.data, content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Booking.objects.count(), 0)

    def test_only_post_allowed(self):
        self.assertEqual(self.client.get(PATH, HTTP_AUTHORIZATION=auth("alice")).status_code, 405)

    def test_overlap_same_room_conflicts(self):
        self.assertEqual(self.post(self.data).status_code, 201)
        for start, end in [("10:00", "11:00"), ("09:30", "10:30"), ("10:30", "11:30"),
                           ("10:15", "10:45"), ("09:00", "12:00"), ("10:59:59", "12:00")]:
            data = dict(self.data, start=f"2030-01-01T{start}Z", end=f"2030-01-01T{end}Z")
            response = self.post(data, "bob")
            self.assertEqual(response.status_code, 409, (start, end))
            self.assertEqual(response.json()["code"], "slot_conflict")
        self.assertEqual(Booking.objects.count(), 1)

    def test_same_instant_other_offset_conflicts(self):
        self.assertEqual(self.post(self.data).status_code, 201)
        data = dict(self.data, start="2030-01-01T12:30:00+02:00", end="2030-01-01T13:30:00+02:00")
        self.assertEqual(self.post(data, "bob").status_code, 409)

    def test_adjacent_and_other_room_allowed(self):
        self.assertEqual(self.post(self.data).status_code, 201)
        after = dict(self.data, start="2030-01-01T11:00:00Z", end="2030-01-01T12:00:00Z")
        before = dict(self.data, start="2030-01-01T09:00:00Z", end="2030-01-01T10:00:00Z")
        other = dict(self.data, room_id=self.r101.pk)
        for data in (after, before, other):
            self.assertEqual(self.post(data, "bob").status_code, 201, data)
        self.assertEqual(Booking.objects.count(), 4)

    def test_inactive_bookings_do_not_block(self):
        for status in ("CANCELLED", "REJECTED"):
            Booking.objects.create(room=self.r100, owner=self.bob, start=at(10), end=at(11), status=status)
        self.assertEqual(self.post(self.data).status_code, 201)

    def test_malformed_body_is_400(self):
        cases = ["", "not json", "{", "[]", "null", '"text"', "42"]
        for raw in cases:
            response = self.post(None, raw=raw)
            self.assertEqual(response.status_code, 400, raw)
            self.assertEqual(response.json()["code"], "invalid_input")
        self.assertEqual(Booking.objects.count(), 0)

    def test_invalid_fields_are_400(self):
        cases = [
            {k: v for k, v in self.data.items() if k != "room_id"},
            {k: v for k, v in self.data.items() if k != "start"},
            {k: v for k, v in self.data.items() if k != "end"},
            dict(self.data, room_id=str(self.r100.pk)),
            dict(self.data, room_id=True),
            dict(self.data, room_id=None),
            dict(self.data, room_id=1.5),
            dict(self.data, room_id=0),
            dict(self.data, start="2030-01-01T10:00:00"),           # naive
            dict(self.data, end="2030-01-01"),                      # date only
            dict(self.data, start="garbage"),
            dict(self.data, start=123),
            dict(self.data, end=self.data["start"]),                # start == end
            dict(self.data, start=self.data["end"], end=self.data["start"]),  # start > end
        ]
        for data in cases:
            response = self.post(data)
            self.assertEqual(response.status_code, 400, data)
            self.assertEqual(response.json()["code"], "invalid_input")
        self.assertEqual(Booking.objects.count(), 0)

    def test_unknown_room_is_404(self):
        response = self.post(dict(self.data, room_id=self.r101.pk + 1000))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")
        self.assertEqual(Booking.objects.count(), 0)

    def test_database_rejects_overlap_without_api(self):
        Booking.objects.create(room=self.r100, owner=self.alice, start=at(10), end=at(11))
        with self.assertRaises(IntegrityError) as caught, transaction.atomic():
            Booking.objects.create(room=self.r100, owner=self.bob, start=at(10, 30), end=at(11, 30))
        self.assertTrue(is_overlap_violation(caught.exception))
        self.assertIn(OVERLAP_CONSTRAINT, str(caught.exception))

    def test_other_integrity_errors_are_not_overlap(self):
        with self.assertRaises(IntegrityError) as caught, transaction.atomic():
            Booking.objects.create(room=self.r100, owner=self.alice, start=at(11), end=at(10))
        self.assertFalse(is_overlap_violation(caught.exception))

    def test_constraints_present_in_database(self):
        with connection.cursor() as cursor:
            names = set(connection.introspection.get_constraints(cursor, Booking._meta.db_table))
            cursor.execute("SELECT 1 FROM pg_extension WHERE extname = 'btree_gist'")
            self.assertIsNotNone(cursor.fetchone())
        self.assertTrue({"booking_start_before_end", "booking_status_valid", OVERLAP_CONSTRAINT} <= names)


class ConcurrentCreate(TransactionTestCase):
    """Two threads, two PostgreSQL connections, released at the same moment."""

    START = "2031-04-01T08:00:00Z"
    END = "2031-04-01T09:00:00Z"

    def setUp(self):
        self.room = Room.objects.create(name="race", capacity=100)
        for name in ("one", "two"):
            get_user_model().objects.create_user(name, password="race")

    def race(self, payloads):
        barrier = Barrier(len(payloads), timeout=15)

        def attempt(item):
            name, data = item
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pid = cursor.fetchone()[0]
                barrier.wait()
                response = Client().post(PATH, data, content_type="application/json",
                                         HTTP_AUTHORIZATION=auth(name, "race"))
                return pid, response.status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
            results = list(pool.map(attempt, payloads, timeout=30))
        pids = [pid for pid, _ in results]
        self.assertEqual(len(set(pids)), len(pids), "threads must use distinct DB connections")
        return sorted(status for _, status in results)

    def test_simultaneous_same_slot_one_wins(self):
        data = {"room_id": self.room.pk, "start": self.START, "end": self.END}
        for attempt in range(5):
            Booking.objects.all().delete()
            statuses = self.race([("one", data), ("two", data)])
            self.assertEqual(statuses, [201, 409], f"attempt {attempt}")
            self.assertEqual(Booking.objects.filter(room=self.room, status="ACTIVE").count(), 1)

    def test_simultaneous_partial_overlap_one_wins(self):
        first = {"room_id": self.room.pk, "start": self.START, "end": self.END}
        second = dict(first, start="2031-04-01T08:30:00Z", end="2031-04-01T09:30:00Z")
        self.assertEqual(self.race([("one", first), ("two", second)]), [201, 409])
        self.assertEqual(Booking.objects.filter(room=self.room).count(), 1)

    def test_simultaneous_adjacent_both_win(self):
        first = {"room_id": self.room.pk, "start": self.START, "end": self.END}
        second = dict(first, start=self.END, end="2031-04-01T10:00:00Z")
        self.assertEqual(self.race([("one", first), ("two", second)]), [201, 201])
        self.assertEqual(Booking.objects.filter(room=self.room).count(), 2)
