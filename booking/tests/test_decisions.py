"""v2 staff queue, decisions and owner cancellation of PENDING (KAN-15 / B5)."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

from django.contrib.auth import get_user_model
from django.db import connection, connections
from django.test import Client, TestCase, TransactionTestCase

from booking.models import Booking, Room

QUEUE = "/api/approvals/"
UTC = timezone.utc


def at(hour):
    return datetime(2031, 5, 1, hour, tzinfo=UTC)


def decision_path(pk):
    return f"/api/approvals/{pk}/decision/"


def cancel_path(pk):
    return f"/api/bookings/{pk}/cancel/"


def auth(name):
    return "Basic " + base64.b64encode(f"{name}:test".encode()).decode()


class Fixtures:
    def make_world(self):
        users = get_user_model().objects
        self.alice = users.create_user("alice", password="test")
        users.create_user("bob", password="test")
        self.admin = users.create_user("admin", password="test", is_staff=True)
        self.room = Room.objects.create(name="R101", capacity=101)

    def pending(self, hour=10):
        return Booking.objects.create(owner=self.alice, room=self.room, start=at(hour), end=at(hour + 1),
                                      status=Booking.Status.PENDING)

    def post(self, path, user="admin", data=None):
        return Client().post(path, data or {}, content_type="application/json", HTTP_AUTHORIZATION=auth(user))

    def decide(self, row, decision, user="admin"):
        return self.post(decision_path(row.pk), user, {"decision": decision})


class Decisions(Fixtures, TestCase):
    def setUp(self):
        self.make_world()

    def test_queue_staff_only_and_lists_pending_only(self):
        pending = self.pending(10)
        Booking.objects.create(owner=self.alice, room=self.room, start=at(12), end=at(13))
        self.assertEqual(Client().get(QUEUE).status_code, 401)
        self.assertEqual(Client().get(QUEUE, HTTP_AUTHORIZATION=auth("alice")).status_code, 403)
        response = Client().get(QUEUE, HTTP_AUTHORIZATION=auth("admin"))
        self.assertEqual([r["id"] for r in response.json()], [pending.pk])
        self.assertEqual(self.post(QUEUE).status_code, 405)

    def test_approve_records_audit(self):
        row = self.pending()
        response = self.decide(row, "approve")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual((body["status"], body["decision"], body["decided_by"]), ("ACTIVE", "approve", self.admin.pk))
        row.refresh_from_db()
        self.assertEqual((row.status, row.decision, row.decided_by), ("ACTIVE", "approve", self.admin))
        self.assertIsNotNone(row.decided_at)
        self.assertEqual(body["decided_at"], row.decided_at.isoformat())
        self.assertEqual(self.client.get(QUEUE, HTTP_AUTHORIZATION=auth("admin")).json(), [])

    def test_reject_releases_slot_and_keeps_audit(self):
        row = self.pending()
        self.assertEqual(self.decide(row, "reject").json()["status"], "REJECTED")
        row.refresh_from_db()
        self.assertEqual((row.status, row.decision, row.decided_by), ("REJECTED", "reject", self.admin))
        # Slot is free again: a new booking for the same interval succeeds.
        Booking.objects.create(owner=self.alice, room=self.room, start=at(10), end=at(11),
                               status=Booking.Status.PENDING)

    def test_pending_holds_slot_until_rejected(self):
        from django.db import IntegrityError, transaction
        row = self.pending()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Booking.objects.create(owner=self.alice, room=self.room, start=at(10), end=at(11))
        self.decide(row, "reject")

    def test_same_decision_retry_is_stable(self):
        for decision in ("approve", "reject"):
            row = self.pending(14 if decision == "approve" else 16)
            first = self.decide(row, decision)
            second = self.decide(row, decision)
            self.assertEqual((first.status_code, second.status_code), (200, 200))
            self.assertEqual(first.json(), second.json())

    def test_retry_by_other_staff_keeps_original_actor(self):
        other = get_user_model().objects.create_user("boss", password="test", is_staff=True)
        row = self.pending()
        first = self.decide(row, "approve").json()
        again = self.post(decision_path(row.pk), "boss", {"decision": "approve"})
        self.assertEqual(again.json(), first)
        self.assertNotEqual(again.json()["decided_by"], other.pk)

    def test_opposite_decision_is_409_and_audit_unchanged(self):
        for first, opposite in (("approve", "reject"), ("reject", "approve")):
            row = self.pending(10 if first == "approve" else 12)
            self.decide(row, first)
            row.refresh_from_db()
            snapshot = (row.status, row.decision, row.decided_by_id, row.decided_at)
            response = self.decide(row, opposite)
            self.assertEqual((response.status_code, response.json()["code"]), (409, "invalid_transition"))
            row.refresh_from_db()
            self.assertEqual(snapshot, (row.status, row.decision, row.decided_by_id, row.decided_at))

    def test_validation_and_permissions(self):
        row = self.pending()
        self.assertEqual(self.decide(row, "approve", "alice").status_code, 403)
        self.assertEqual(self.decide(row, "approve", "bob").status_code, 403)
        self.assertEqual(Client().post(decision_path(row.pk), {}, content_type="application/json").status_code, 401)
        self.assertEqual(self.decide(row, "maybe").status_code, 400)
        self.assertEqual(self.post(decision_path(row.pk), data={}).status_code, 400)
        self.assertEqual(self.post(decision_path(row.pk), data={"decision": ["approve"]}).status_code, 400)
        self.assertEqual(self.decide(Booking(pk=999999), "approve").status_code, 404)
        self.assertEqual(Client().get(decision_path(row.pk), HTTP_AUTHORIZATION=auth("admin")).status_code, 405)
        row.refresh_from_db()
        self.assertEqual((row.status, row.decision), ("PENDING", None))

    def test_legacy_active_and_cancelled_cannot_be_decided(self):
        legacy = Booking.objects.create(owner=self.alice, room=self.room, start=at(8), end=at(9))
        gone = Booking.objects.create(owner=self.alice, room=self.room, start=at(6), end=at(7),
                                      status=Booking.Status.CANCELLED)
        for row in (legacy, gone):
            for decision in ("approve", "reject"):
                self.assertEqual(self.decide(row, decision).status_code, 409)
            row.refresh_from_db()
            self.assertIsNone(row.decision)
        self.assertEqual(legacy.status, "ACTIVE")

    def test_owner_cancels_pending_then_decision_is_409(self):
        row = self.pending()
        self.assertEqual(self.post(cancel_path(row.pk), "bob").status_code, 403)
        response = self.post(cancel_path(row.pk), "alice")
        self.assertEqual((response.status_code, response.json()["status"]), (200, "CANCELLED"))
        row.refresh_from_db()
        self.assertEqual((row.status, row.decision, row.decided_by), ("CANCELLED", None, None))
        self.assertEqual(self.decide(row, "approve").status_code, 409)
        self.assertEqual(self.client.get(QUEUE, HTTP_AUTHORIZATION=auth("admin")).json(), [])
        # Slot is released immediately.
        Booking.objects.create(owner=self.alice, room=self.room, start=at(10), end=at(11))

    def test_cancel_after_decision(self):
        row = self.pending()
        self.decide(row, "approve")
        self.assertEqual(self.post(cancel_path(row.pk), "alice").json()["status"], "CANCELLED")
        self.assertEqual(self.decide(row, "approve").status_code, 409)
        rejected = self.pending(14)
        self.decide(rejected, "reject")
        self.assertEqual(self.post(cancel_path(rejected.pk), "alice").status_code, 409)


class DatabaseGuarantees(Fixtures, TestCase):
    """Гарантії рівня БД з A5, на які спирається B5."""

    def setUp(self):
        self.make_world()

    def test_partial_audit_rejected_by_check_constraint(self):
        from django.db import IntegrityError, transaction
        row = self.pending()
        for fields in ({"decision": "approve"}, {"decided_by": self.admin}, {"decided_at": at(9)}):
            with self.assertRaises(IntegrityError), transaction.atomic():
                Booking.objects.filter(pk=row.pk).update(**fields)
        row.refresh_from_db()
        self.assertEqual((row.decision, row.decided_by, row.decided_at), (None, None, None))

    def test_exclusion_constraint_covers_pending_not_rejected(self):
        from django.db import IntegrityError, transaction
        self.pending()
        with self.assertRaises(IntegrityError), transaction.atomic():
            Booking.objects.create(owner=self.alice, room=self.room, start=at(10), end=at(11),
                                   status=Booking.Status.PENDING)
        Booking.objects.create(owner=self.alice, room=self.room, start=at(10), end=at(11),
                               status=Booking.Status.REJECTED, decision="reject",
                               decided_by=self.admin, decided_at=at(9))

    def test_pending_is_cancellable(self):
        from booking.flow import CANCELLABLE
        self.assertIn(Booking.Status.PENDING, CANCELLABLE)


class ConcurrentDecisions(Fixtures, TransactionTestCase):
    """Threads with distinct PostgreSQL connections released at the same moment."""

    def setUp(self):
        self.make_world()

    def race(self, row, actions):
        barrier = Barrier(len(actions), timeout=15)

        def attempt(action):
            kind, user, value = action
            try:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT pg_backend_pid()")
                    pid = cursor.fetchone()[0]
                barrier.wait()
                if kind == "decide":
                    response = self.decide(row, value, user)
                else:
                    response = self.post(cancel_path(row.pk), user)
                return pid, response.status_code, response.json()
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(actions)) as pool:
            results = list(pool.map(attempt, actions, timeout=30))
        pids = [r[0] for r in results]
        self.assertEqual(len(set(pids)), len(pids), "threads must use distinct DB connections")
        row.refresh_from_db()
        return [(r[1], r[2]) for r in results]

    def test_opposite_decisions_one_wins(self):
        for attempt in range(5):
            Booking.objects.all().delete()
            row = self.pending()
            results = self.race(row, [("decide", "admin", "approve"), ("decide", "admin", "reject")])
            self.assertEqual(sorted(s for s, _ in results), [200, 409], f"attempt {attempt}")
            winner = next(body for status, body in results if status == 200)
            self.assertEqual(winner["decision"], row.decision)
            self.assertEqual(winner["status"], row.status)
            self.assertEqual(winner["decided_at"], row.decided_at.isoformat())
            self.assertEqual(row.decided_by, self.admin)
            if row.decision == "reject":
                # Released slot can be rebooked; approved slot stays held.
                Booking.objects.create(owner=self.alice, room=self.room, start=at(10), end=at(11))
            else:
                self.assertEqual(Booking.objects.filter(room=self.room, status="ACTIVE").count(), 1)

    def test_same_decision_race_identical_audit(self):
        row = self.pending()
        results = self.race(row, [("decide", "admin", "approve")] * 2)
        self.assertEqual([s for s, _ in results], [200, 200])
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(results[0][1]["decided_at"], row.decided_at.isoformat())

    def test_cancel_versus_decision_exactly_one_outcome(self):
        for decision in ("approve", "reject"):
            for attempt in range(3):
                Booking.objects.all().delete()
                row = self.pending()
                results = self.race(row, [("decide", "admin", decision), ("cancel", "alice", None)])
                codes = sorted(s for s, _ in results)
                state = (row.status, row.decision)
                if state == ("CANCELLED", None):
                    # Скасування виграло: рішення бачить CANCELLED -> 409, аудиту немає.
                    self.assertEqual(codes, [200, 409])
                    self.assertIsNone(row.decided_by)
                elif state == ("CANCELLED", "approve"):
                    # Спочатку схвалення, потім скасування ACTIVE: обидва 200, аудит збережено.
                    self.assertEqual((decision, codes), ("approve", [200, 200]))
                    self.assertEqual(row.decided_by, self.admin)
                elif state == ("REJECTED", "reject"):
                    # Спочатку відхилення: скасування REJECTED -> 409, аудит збережено.
                    self.assertEqual((decision, codes), ("reject", [200, 409]))
                    self.assertEqual(row.decided_by, self.admin)
                else:
                    self.fail(f"unexpected final state {state} for {decision}")
