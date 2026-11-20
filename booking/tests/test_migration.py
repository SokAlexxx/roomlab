"""Schema v1 -> v2 migration test (KAN-8 / A5).

Real migrations are applied with MigrationExecutor on the PostgreSQL test
database: rows are written through the *historical* v1 model (state 0002),
then 0003 is applied and the same rows are read back through the v2 model.
TransactionTestCase is required because migrations run DDL outside the
per-test transaction of TestCase. tearDown always migrates back to the
latest schema so the rest of the suite sees the current models.
"""
from datetime import datetime, timezone

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

from booking.models import OVERLAP_CONSTRAINT

V1 = [("booking", "0002_booking_no_overlap_active")]
V2 = [("booking", "0003_booking_decision_audit")]
FIELDS = ("id", "owner_id", "room_id", "start", "end", "status")
UTC = timezone.utc


def at(hour):
    return datetime(2030, 1, 1, hour, tzinfo=UTC)


def migrate(targets):
    """Apply migrations up/down to targets; return the historical apps registry for that state."""
    executor = MigrationExecutor(connection)
    executor.loader.build_graph()
    executor.migrate(targets)
    executor.loader.build_graph()
    return executor.loader.project_state(targets).apps


class MigrationV1ToV2(TransactionTestCase):
    def setUp(self):
        apps = migrate(V1)
        User = apps.get_model(settings.AUTH_USER_MODEL)
        Room = apps.get_model("booking", "Room")
        Booking = apps.get_model("booking", "Booking")
        self.alice = User.objects.create(username="alice")
        self.bob = User.objects.create(username="bob")
        self.r100 = Room.objects.create(name="R100", capacity=100)
        self.r101 = Room.objects.create(name="R101", capacity=101)
        # Legacy v1 data: ACTIVE and CANCELLED, including an ACTIVE on R101 (PENDING-for-new in v2)
        # and a CANCELLED row overlapping an ACTIVE one (legal in v1, must stay legal in v2).
        rows = [
            (self.alice, self.r101, 10, 11, "ACTIVE"),
            (self.bob, self.r101, 10, 11, "CANCELLED"),
            (self.bob, self.r100, 9, 12, "ACTIVE"),
            (self.alice, self.r100, 8, 10, "CANCELLED"),
        ]
        for owner, room, start, end, status in rows:
            Booking.objects.create(owner=owner, room=room, start=at(start), end=at(end), status=status)
        self.before = list(Booking.objects.order_by("id").values(*FIELDS))

    def tearDown(self):
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_v2_preserves_legacy_rows_and_leaves_audit_empty(self):
        Booking = migrate(V2).get_model("booking", "Booking")
        self.assertEqual(list(Booking.objects.order_by("id").values(*FIELDS)), self.before)
        self.assertEqual({r["status"] for r in self.before}, {"ACTIVE", "CANCELLED"})
        # No retroactive approval: legacy rows keep their status and carry no audit.
        self.assertFalse(Booking.objects.exclude(decided_by=None, decided_at=None, decision=None).exists())
        self.assertFalse(Booking.objects.filter(status__in=["PENDING", "REJECTED"]).exists())

    def test_v2_overlap_covers_pending_but_not_inactive(self):
        Booking = migrate(V2).get_model("booking", "Booking")
        # PENDING over a legacy ACTIVE slot is rejected by the widened exclusion constraint.
        with self.assertRaises(IntegrityError) as caught, transaction.atomic():
            Booking.objects.create(owner_id=self.bob.pk, room_id=self.r101.pk, start=at(10), end=at(11),
                                   status="PENDING")
        self.assertIn(OVERLAP_CONSTRAINT, str(caught.exception))
        # PENDING vs PENDING is rejected too; adjacency stays legal.
        Booking.objects.create(owner_id=self.bob.pk, room_id=self.r101.pk, start=at(11), end=at(12),
                               status="PENDING")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Booking.objects.create(owner_id=self.alice.pk, room_id=self.r101.pk, start=at(11), end=at(12),
                                   status="PENDING")
        # REJECTED and CANCELLED never block.
        Booking.objects.create(owner_id=self.alice.pk, room_id=self.r101.pk, start=at(11), end=at(12),
                               status="REJECTED")
        self.assertEqual(list(Booking.objects.filter(pk__in=[r["id"] for r in self.before])
                              .order_by("id").values(*FIELDS)), self.before)

    def test_v2_audit_constraints(self):
        Booking = migrate(V2).get_model("booking", "Booking")
        pk = self.before[0]["id"]
        partial = {"decided_at": at(9)}
        bad_value = {"decided_by_id": self.bob.pk, "decided_at": at(9), "decision": "maybe"}
        for values, name in ((partial, "booking_decision_audit_complete"), (bad_value, "booking_decision_valid")):
            with self.assertRaises(IntegrityError) as caught, transaction.atomic():
                Booking.objects.filter(pk=pk).update(**values)
            self.assertIn(name, str(caught.exception))
        self.assertEqual(Booking.objects.values(*FIELDS).get(pk=pk), self.before[0])

    def test_rollback_to_v1_keeps_legacy_rows(self):
        migrate(V2)
        Booking = migrate(V1).get_model("booking", "Booking")
        self.assertEqual(list(Booking.objects.order_by("id").values(*FIELDS)), self.before)
        self.assertNotIn("decision", {f.name for f in Booking._meta.get_fields()})
