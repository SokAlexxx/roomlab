"""v2 create status by capacity (KAN-9 / A6).

Capacity > 100 -> PENDING, otherwise ACTIVE. PENDING reserves the slot in the
availability search and in the DB exclusion constraint; CANCELLED/REJECTED free it.
Legacy rows are never re-evaluated.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase

from booking.models import Booking, Room
from booking.rooms import APPROVAL_CAPACITY_THRESHOLD, initial_status

CREATE = "/api/bookings/"
SEARCH = "/api/rooms/available/"
START = "2030-01-01T10:00:00Z"
END = "2030-01-01T11:00:00Z"
UTC = timezone.utc


def at(hour):
    return datetime(2030, 1, 1, hour, tzinfo=UTC)


def auth(name, password="test"):
    return "Basic " + base64.b64encode(f"{name}:{password}".encode()).decode()


class CapacityStatus(TestCase):
    def setUp(self):
        self.r99 = Room.objects.create(name="R99", capacity=99)
        self.r100 = Room.objects.create(name="R100", capacity=100)
        self.r101 = Room.objects.create(name="R101", capacity=101)
        self.alice = get_user_model().objects.create_user("alice", password="test")
        self.bob = get_user_model().objects.create_user("bob", password="test")

    def post(self, room, user="alice", start=START, end=END):
        return self.client.post(CREATE, {"room_id": room.pk, "start": start, "end": end},
                                content_type="application/json", HTTP_AUTHORIZATION=auth(user))

    def available(self, start=START, end=END):
        response = self.client.get(SEARCH, {"start": start, "end": end}, HTTP_AUTHORIZATION=auth("alice"))
        self.assertEqual(response.status_code, 200, response.content)
        return [r["id"] for r in response.json()]

    def cancel(self, booking_id, user="alice"):
        return self.client.post(f"/api/bookings/{booking_id}/cancel/", {}, content_type="application/json",
                                HTTP_AUTHORIZATION=auth(user))

    def test_threshold_is_100(self):
        self.assertEqual(APPROVAL_CAPACITY_THRESHOLD, 100)

    def test_status_by_capacity_99_100_101(self):
        for room, expected in ((self.r99, "ACTIVE"), (self.r100, "ACTIVE"), (self.r101, "PENDING")):
            with self.subTest(capacity=room.capacity):
                self.assertEqual(initial_status(room), expected)
                response = self.post(room)
                self.assertEqual(response.status_code, 201, response.content)
                body = response.json()
                row = Booking.objects.get(pk=body["id"])
                self.assertEqual(body, {"id": row.pk, "room_id": room.pk, "start": "2030-01-01T10:00:00+00:00",
                                        "end": "2030-01-01T11:00:00+00:00", "status": expected})
                self.assertEqual((row.status, row.owner), (expected, self.alice))
                # A new PENDING has no audit: it has not been decided yet.
                self.assertEqual((row.decided_by, row.decided_at, row.decision), (None, None, None))

    def test_pending_blocks_search_and_create(self):
        self.assertEqual(self.post(self.r101).json()["status"], "PENDING")
        self.assertEqual(self.available(), [self.r99.pk, self.r100.pk])
        response = self.post(self.r101, "bob", "2030-01-01T10:30:00Z", "2030-01-01T11:30:00Z")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "slot_conflict")
        self.assertEqual(Booking.objects.filter(room=self.r101).count(), 1)

    def test_search_excludes_both_occupying_states(self):
        Booking.objects.create(room=self.r99, owner=self.bob, start=at(10), end=at(11), status="ACTIVE")
        Booking.objects.create(room=self.r101, owner=self.bob, start=at(10), end=at(11), status="PENDING")
        self.assertEqual(self.available(), [self.r100.pk])
        # Adjacent intervals stay free for both states: [start, end).
        self.assertEqual(self.available("2030-01-01T11:00:00Z", "2030-01-01T12:00:00Z"),
                         [self.r99.pk, self.r100.pk, self.r101.pk])

    def test_adjacent_pending_allowed(self):
        self.assertEqual(self.post(self.r101).status_code, 201)
        response = self.post(self.r101, "bob", END, "2030-01-01T12:00:00Z")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["status"], "PENDING")

    def test_cancelled_pending_releases_slot(self):
        first = self.post(self.r101).json()
        self.assertEqual(self.cancel(first["id"]).json()["status"], "CANCELLED")
        self.assertIn(self.r101.pk, self.available())
        second = self.post(self.r101, "bob")
        self.assertEqual(second.status_code, 201)
        self.assertEqual(second.json()["status"], "PENDING")

    def test_rejected_releases_slot(self):
        Booking.objects.create(room=self.r101, owner=self.bob, start=at(10), end=at(11), status="REJECTED")
        self.assertIn(self.r101.pk, self.available())
        self.assertEqual(self.post(self.r101).json()["status"], "PENDING")

    def test_cancelled_active_releases_slot_on_small_room(self):
        first = self.post(self.r100).json()
        self.assertEqual(self.cancel(first["id"]).status_code, 200)
        self.assertEqual(self.post(self.r100, "bob").json()["status"], "ACTIVE")


class LegacyRecords(TestCase):
    """Rows created before v2 keep their status, ID and data; nothing is re-evaluated."""

    def setUp(self):
        self.r101 = Room.objects.create(name="R101", capacity=101)
        self.r100 = Room.objects.create(name="R100", capacity=100)
        self.alice = get_user_model().objects.create_user("alice", password="test")
        get_user_model().objects.create_user("bob", password="test")
        self.legacy = Booking.objects.create(room=self.r101, owner=self.alice, start=at(10), end=at(11),
                                             status="ACTIVE")
        self.snapshot = Booking.objects.filter(pk=self.legacy.pk).values().get()

    def post(self, room, user, start=START, end=END):
        return self.client.post(CREATE, {"room_id": room.pk, "start": start, "end": end},
                                content_type="application/json", HTTP_AUTHORIZATION=auth(user))

    def assert_legacy_unchanged(self):
        self.assertEqual(Booking.objects.filter(pk=self.legacy.pk).values().get(), self.snapshot)

    def test_legacy_active_on_large_room_blocks_new_pending(self):
        response = self.post(self.r101, "bob")
        self.assertEqual(response.status_code, 409)
        self.assert_legacy_unchanged()

    def test_new_bookings_do_not_touch_legacy(self):
        self.assertEqual(self.post(self.r101, "bob", END, "2030-01-01T12:00:00Z").json()["status"], "PENDING")
        self.assertEqual(self.post(self.r100, "bob").json()["status"], "ACTIVE")
        self.assert_legacy_unchanged()
        mine = self.client.get("/api/bookings/mine/", HTTP_AUTHORIZATION=auth("alice")).json()
        self.assertEqual([(r["id"], r["status"]) for r in mine], [(self.legacy.pk, "ACTIVE")])

    def test_capacity_change_is_not_retroactive(self):
        Room.objects.filter(pk=self.r100.pk).update(capacity=150)
        old = Booking.objects.create(room=self.r100, owner=self.alice, start=at(12), end=at(13), status="ACTIVE")
        Room.objects.filter(pk=self.r101.pk).update(capacity=50)
        self.assert_legacy_unchanged()
        old.refresh_from_db()
        self.assertEqual(old.status, "ACTIVE")
        # Only new requests see the current capacity.
        self.assertEqual(self.post(self.r101, "bob", END, "2030-01-01T12:00:00Z").json()["status"], "ACTIVE")
        self.assertEqual(self.post(self.r100, "bob", "2030-01-01T14:00:00Z", "2030-01-01T15:00:00Z")
                         .json()["status"], "PENDING")


class ConcurrentPending(TransactionTestCase):
    """Parallel creates on a capacity-101 room over two real PostgreSQL connections."""

    START = "2031-04-01T08:00:00Z"
    END = "2031-04-01T09:00:00Z"

    def setUp(self):
        self.room = Room.objects.create(name="big", capacity=101)
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
                response = Client().post(CREATE, data, content_type="application/json",
                                         HTTP_AUTHORIZATION=auth(name, "race"))
                return pid, response.status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
            results = list(pool.map(attempt, payloads, timeout=30))
        pids = [pid for pid, _ in results]
        self.assertEqual(len(set(pids)), len(pids), "threads must use distinct DB connections")
        return sorted(status for _, status in results)

    def test_simultaneous_same_slot_one_pending(self):
        data = {"room_id": self.room.pk, "start": self.START, "end": self.END}
        for attempt in range(5):
            Booking.objects.all().delete()
            self.assertEqual(self.race([("one", data), ("two", data)]), [201, 409], f"attempt {attempt}")
            self.assertEqual(list(Booking.objects.filter(room=self.room).values_list("status", flat=True)),
                             ["PENDING"])

    def test_simultaneous_partial_overlap_one_pending(self):
        first = {"room_id": self.room.pk, "start": self.START, "end": self.END}
        second = dict(first, start="2031-04-01T08:30:00Z", end="2031-04-01T09:30:00Z")
        self.assertEqual(self.race([("one", first), ("two", second)]), [201, 409])
        self.assertEqual(Booking.objects.filter(room=self.room, status="PENDING").count(), 1)

    def test_simultaneous_adjacent_both_pending(self):
        first = {"room_id": self.room.pk, "start": self.START, "end": self.END}
        second = dict(first, start=self.END, end="2031-04-01T10:00:00Z")
        self.assertEqual(self.race([("one", first), ("two", second)]), [201, 201])
        self.assertEqual(Booking.objects.filter(room=self.room, status="PENDING").count(), 2)
