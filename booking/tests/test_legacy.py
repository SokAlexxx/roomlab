"""Characterization tests of v1 behaviour that must survive v2 (KAN-14 / B4).

They pin what v1 does today and stay valid after the v2 approval flow lands:
* every API create uses R100 (capacity 100), which remains auto-ACTIVE in v2;
* a legacy ACTIVE booking on R101 (capacity 101, PENDING-for-new in v2) is
  inserted through the ORM, as it would exist in the database before the v2
  migration, and must be read back unchanged, keep blocking its slot and stay
  cancellable by its owner only. No retroactive approval.
Nothing here asserts behaviour that v2 intentionally changes (new R101
bookings, approvals endpoints).
"""
import base64
import os
from datetime import datetime, timezone
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase

from booking.models import Booking, Room

CREATE = "/api/bookings/"
MINE = "/api/bookings/mine/"
AVAILABLE = "/api/rooms/available/"
UTC = timezone.utc


def at(hour):
    return datetime(2030, 1, 1, hour, tzinfo=UTC)


def iso(hour):
    return f"2030-01-01T{hour:02d}:00:00Z"


def cancel_path(pk):
    return f"/api/bookings/{pk}/cancel/"


class LegacyBase(TestCase):
    def setUp(self):
        users = get_user_model().objects
        self.alice = users.create_user("alice", password="test")
        self.bob = users.create_user("bob", password="test")
        self.admin = users.create_user("admin", password="test", is_staff=True)
        self.r100 = Room.objects.create(name="R100", capacity=100)
        self.r101 = Room.objects.create(name="R101", capacity=101)
        # Legacy v1 row on the room that v2 routes to approval.
        self.legacy = Booking.objects.create(owner=self.alice, room=self.r101, start=at(10), end=at(11),
                                             status=Booking.Status.ACTIVE)
        self.legacy_record = {"id": self.legacy.pk, "room_id": self.r101.pk, "start": at(10).isoformat(),
                              "end": at(11).isoformat(), "status": "ACTIVE"}

    def auth(self, user):
        return {"HTTP_AUTHORIZATION": "Basic " + base64.b64encode(f"{user}:test".encode()).decode()}

    def get(self, path, user="alice", data=None):
        return self.client.get(path, data or {}, **self.auth(user))

    def post(self, path, data=None, user="alice"):
        return self.client.post(path, data or {}, content_type="application/json", **self.auth(user))

    def create_r100(self, start, end, user="alice"):
        return self.post(CREATE, {"room_id": self.r100.pk, "start": iso(start), "end": iso(end)}, user)

    def free_ids(self, start, end):
        response = self.get(AVAILABLE, data={"start": iso(start), "end": iso(end)})
        self.assertEqual(response.status_code, 200)
        return [r["id"] for r in response.json()]

    def snapshot(self):
        return Booking.objects.values().get(pk=self.legacy.pk)


class V1OnR100(LegacyBase):
    """v1 contract on R100; v2 keeps capacity <= 100 auto-ACTIVE."""

    def test_create_returns_active_record(self):
        response = self.create_r100(12, 13)
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(set(body), {"id", "room_id", "start", "end", "status"})
        self.assertEqual(body, {"id": body["id"], "room_id": self.r100.pk, "start": "2030-01-01T12:00:00+00:00",
                                "end": "2030-01-01T13:00:00+00:00", "status": "ACTIVE"})
        row = Booking.objects.get(pk=body["id"])
        self.assertEqual((row.owner, row.status), (self.alice, "ACTIVE"))

    def test_conflict_and_adjacent(self):
        self.assertEqual(self.create_r100(12, 13).status_code, 201)
        conflict = self.create_r100(12, 13, "bob")
        self.assertEqual((conflict.status_code, conflict.json()["code"]), (409, "slot_conflict"))
        self.assertEqual(self.create_r100(13, 14, "bob").status_code, 201)
        self.assertEqual(self.create_r100(11, 12, "bob").status_code, 201)
        self.assertEqual(Booking.objects.filter(room=self.r100).count(), 3)

    def test_invalid_input_and_missing_room(self):
        bad = [{}, {"room_id": self.r100.pk, "start": iso(12), "end": iso(12)},
               {"room_id": self.r100.pk, "start": "2030-01-01T12:00:00", "end": "2030-01-01T13:00:00"}]
        for data in bad:
            response = self.post(CREATE, data)
            self.assertEqual((response.status_code, response.json()["code"]), (400, "invalid_input"), data)
        missing = self.post(CREATE, {"room_id": self.r101.pk + 1000, "start": iso(12), "end": iso(13)})
        self.assertEqual((missing.status_code, missing.json()["code"]), (404, "not_found"))
        self.assertEqual(Booking.objects.count(), 1)

    def test_search_cancel_lifecycle(self):
        self.assertEqual(self.free_ids(12, 13), [self.r100.pk, self.r101.pk])
        created = self.create_r100(12, 13).json()
        self.assertEqual(self.free_ids(12, 13), [self.r101.pk])
        self.assertEqual(self.post(cancel_path(created["id"]), user="bob").status_code, 403)
        cancelled = self.post(cancel_path(created["id"]))
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json(), dict(created, status="CANCELLED"))
        self.assertEqual(self.free_ids(12, 13), [self.r100.pk, self.r101.pk])
        again = self.post(cancel_path(created["id"]))
        self.assertEqual((again.status_code, again.json()["code"]), (409, "invalid_transition"))

    def test_unauthenticated_is_401(self):
        for response in (self.client.get(MINE), self.client.post(CREATE, {}, content_type="application/json")):
            self.assertEqual(response.status_code, 401)


class LegacyActiveOnR101(LegacyBase):
    """Pre-v2 ACTIVE row on R101 stays readable and behaves exactly as in v1."""

    def test_mine_returns_legacy_unchanged(self):
        before = self.snapshot()
        response = self.get(MINE)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [self.legacy_record])
        self.assertEqual(self.get(MINE, "bob").json(), [])
        self.assertEqual(self.snapshot(), before)

    def test_legacy_still_blocks_its_slot(self):
        self.assertEqual(self.free_ids(10, 11), [self.r100.pk])
        # Adjacent search on R101 is free; legacy keeps [start, end).
        self.assertEqual(self.free_ids(11, 12), [self.r100.pk, self.r101.pk])

    def test_r100_activity_does_not_touch_legacy(self):
        before = self.snapshot()
        created = self.create_r100(10, 11).json()
        self.post(cancel_path(created["id"]))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.get(MINE).json()[0], self.legacy_record)

    def test_foreign_cancel_leaves_legacy_untouched(self):
        before = self.snapshot()
        for user in ("bob", "admin"):
            self.assertEqual(self.post(cancel_path(self.legacy.pk), user=user).status_code, 403)
        self.assertEqual(self.snapshot(), before)

    def test_owner_can_cancel_legacy(self):
        response = self.post(cancel_path(self.legacy.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), dict(self.legacy_record, status="CANCELLED"))
        self.assertEqual(self.free_ids(10, 11), [self.r100.pk, self.r101.pk])

    def test_reseed_preserves_legacy(self):
        before = self.snapshot()
        r101_pk = self.r101.pk
        with mock.patch.dict(os.environ, {"DEMO_PASS": "reseed"}):
            call_command("seed_demo", stdout=StringIO())
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(Room.objects.get(name="R101").pk, r101_pk)
