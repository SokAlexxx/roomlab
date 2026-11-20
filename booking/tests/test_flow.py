"""Own bookings and cancellation (KAN-12 / B2).

Fixtures are created directly through the ORM, so these tests do not depend
on the booking creation endpoint (FR-02).
"""
import base64
from datetime import datetime, timezone

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from booking.models import Booking, Room

MINE = "/api/bookings/mine/"
UTC = timezone.utc


def at(hour):
    return datetime(2030, 1, 1, hour, tzinfo=UTC)


def cancel_path(pk):
    return f"/api/bookings/{pk}/cancel/"


class OwnBookingsFlow(TestCase):
    def setUp(self):
        users = get_user_model().objects
        self.alice = users.create_user("alice", password="test")
        self.bob = users.create_user("bob", password="test")
        self.admin = users.create_user("admin", password="test", is_staff=True)
        self.room = Room.objects.create(name="R100", capacity=100)
        self.other = Room.objects.create(name="R101", capacity=101)
        self.a1 = self.book(self.alice, self.room, 10, 11)
        self.b1 = self.book(self.bob, self.other, 10, 11)

    def book(self, owner, room, start, end, status=Booking.Status.ACTIVE):
        return Booking.objects.create(owner=owner, room=room, start=at(start), end=at(end), status=status)

    def auth(self, user):
        return {"HTTP_AUTHORIZATION": "Basic " + base64.b64encode(f"{user}:test".encode()).decode()}

    def get(self, path, user="alice"):
        return self.client.get(path, **self.auth(user))

    def post(self, path, user="alice"):
        return self.client.post(path, {}, content_type="application/json", **self.auth(user))

    def status_of(self, row):
        row.refresh_from_db()
        return row.status

    # --- GET /api/bookings/mine/ -------------------------------------------

    def test_mine_requires_authentication(self):
        self.assertEqual(self.client.get(MINE).status_code, 401)

    def test_mine_only_get(self):
        self.assertEqual(self.post(MINE).status_code, 405)

    def test_mine_shape(self):
        response = self.get(MINE)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [{
            "id": self.a1.pk, "room_id": self.room.pk, "start": at(10).isoformat(),
            "end": at(11).isoformat(), "status": "ACTIVE",
        }])

    def test_mine_owner_isolation(self):
        self.assertEqual([r["id"] for r in self.get(MINE).json()], [self.a1.pk])
        self.assertEqual([r["id"] for r in self.get(MINE, "bob").json()], [self.b1.pk])
        # Staff gets no special view of other users' bookings here.
        self.assertEqual(self.get(MINE, "admin").json(), [])

    def test_mine_includes_inactive_ordered_by_start(self):
        late = self.book(self.alice, self.room, 14, 15, Booking.Status.CANCELLED)
        early = self.book(self.alice, self.other, 8, 9, Booking.Status.REJECTED)
        pending = self.book(self.alice, self.other, 12, 13, Booking.Status.PENDING)
        rows = self.get(MINE).json()
        self.assertEqual([r["id"] for r in rows], [early.pk, self.a1.pk, pending.pk, late.pk])
        self.assertEqual([r["status"] for r in rows], ["REJECTED", "ACTIVE", "PENDING", "CANCELLED"])

    # --- POST /api/bookings/{id}/cancel/ -----------------------------------

    def test_cancel_requires_authentication(self):
        response = self.client.post(cancel_path(self.a1.pk), {}, content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.status_of(self.a1), "ACTIVE")

    def test_cancel_only_post(self):
        self.assertEqual(self.get(cancel_path(self.a1.pk)).status_code, 405)
        self.assertEqual(self.status_of(self.a1), "ACTIVE")

    def test_owner_cancels(self):
        response = self.post(cancel_path(self.a1.pk))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "id": self.a1.pk, "room_id": self.room.pk, "start": at(10).isoformat(),
            "end": at(11).isoformat(), "status": "CANCELLED",
        })
        self.assertEqual(self.status_of(self.a1), "CANCELLED")
        self.assertEqual(self.get(MINE).json()[0]["status"], "CANCELLED")

    def test_cancel_frees_slot_immediately(self):
        self.post(cancel_path(self.a1.pk))
        response = self.get("/api/rooms/available/?start=2030-01-01T10:00:00Z&end=2030-01-01T11:00:00Z")
        self.assertIn(self.room.pk, [r["id"] for r in response.json()])

    def test_foreign_cancel_is_403_without_mutation(self):
        before = Booking.objects.values().get(pk=self.a1.pk)
        for user in ("bob", "admin"):
            response = self.post(cancel_path(self.a1.pk), user)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json()["code"], "forbidden")
        self.assertEqual(Booking.objects.values().get(pk=self.a1.pk), before)

    def test_foreign_inactive_is_still_403(self):
        # Ownership is checked before the state, so a stranger learns nothing
        # about another user's booking status.
        self.b1.status = Booking.Status.CANCELLED
        self.b1.save()
        self.assertEqual(self.post(cancel_path(self.b1.pk)).status_code, 403)

    def test_unknown_is_404(self):
        missing = Booking.objects.order_by("-pk").first().pk + 1000
        response = self.post(cancel_path(missing))
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")

    def test_duplicate_cancel_is_409(self):
        self.assertEqual(self.post(cancel_path(self.a1.pk)).status_code, 200)
        response = self.post(cancel_path(self.a1.pk))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "invalid_transition")
        self.assertEqual(self.status_of(self.a1), "CANCELLED")

    def test_rejected_is_409(self):
        row = self.book(self.alice, self.room, 12, 13, Booking.Status.REJECTED)
        self.assertEqual(self.post(cancel_path(row.pk)).status_code, 409)
        self.assertEqual(self.status_of(row), "REJECTED")

    def test_owner_cancels_pending(self):
        row = self.book(self.alice, self.other, 12, 13, Booking.Status.PENDING)
        self.assertEqual(self.post(cancel_path(row.pk)).json()["status"], "CANCELLED")

    def test_cancel_touches_only_target(self):
        self.post(cancel_path(self.a1.pk))
        self.assertEqual(self.status_of(self.b1), "ACTIVE")

    def test_cancel_locks_row(self):
        with CaptureQueriesContext(connection) as queries:
            self.post(cancel_path(self.a1.pk))
        self.assertTrue(any("FOR UPDATE" in q["sql"] for q in queries.captured_queries))
