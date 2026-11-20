import base64
from datetime import datetime, timezone
from pathlib import Path
from unittest import skipUnless

from django.contrib.auth import get_user_model
from django.test import TestCase
from booking.models import Booking, Room


def stage(key):
    return skipUnless(Path("evidence", key + ".md").exists(), "later stage: " + key)


class Contract(TestCase):
    def setUp(self):
        self.room = Room.objects.create(name="R100", capacity=100)
        self.big = Room.objects.create(name="R101", capacity=101)
        self.alice = get_user_model().objects.create_user("alice", password="test")
        get_user_model().objects.create_user("bob", password="test")
        get_user_model().objects.create_user("admin", password="test", is_staff=True)
        self.data = dict(room_id=self.room.pk, start="2030-01-01T10:00:00Z", end="2030-01-01T11:00:00Z")

    def call(self, method, path, user="alice", data=None):
        auth = "Basic " + base64.b64encode(f"{user}:test".encode()).decode()
        options = dict(HTTP_AUTHORIZATION=auth) if user else {}
        if method == "get":
            return self.client.get(path, data or {}, **options)
        return self.client.post(path, data or {}, content_type="application/json", **options)

    def row(self):
        return Booking.objects.create(owner=self.alice, room=self.room,
            start=datetime(2030, 1, 1, 10, tzinfo=timezone.utc),
            end=datetime(2030, 1, 1, 11, tzinfo=timezone.utc), status="ACTIVE")

    def test_health(self):
        self.assertEqual(self.client.get("/health/").status_code, 200)

    @stage("A1")
    def test_search(self):
        self.row()
        path = "/api/rooms/available/"
        self.assertEqual(self.call("get", path, None).status_code, 401)
        result = self.call("get", path, data=self.data)
        self.assertEqual([r["id"] for r in result.json()], [self.big.pk])
        edge = dict(start=self.data["end"], end="2030-01-01T12:00:00Z")
        self.assertEqual(len(self.call("get", path, data=edge).json()), 2)
        self.assertEqual(self.call("get", path, data=dict(start=edge["start"], end=edge["start"])).status_code, 400)

    @stage("A2")
    def test_create(self):
        path = "/api/bookings/"
        self.assertEqual(self.call("post", path, data=self.data).status_code, 201)
        self.assertEqual(self.call("post", path, "bob", self.data).status_code, 409)
        self.assertEqual(Booking.objects.count(), 1)

    @stage("B1")
    def test_seed(self):
        import os
        from django.core.management import call_command
        call_command("seed_demo")
        call_command("seed_demo")
        self.assertEqual(Room.objects.filter(name="R101").count(), 1)
        self.assertTrue(get_user_model().objects.get(username="alice").check_password(os.environ["DEMO_PASS"]))
        self.assertFalse(get_user_model().objects.get(username="bob").is_staff)

    @stage("B2")
    def test_owner(self):
        row = self.row()
        mine = "/api/bookings/mine/"
        cancel = f"/api/bookings/{row.pk}/cancel/"
        self.assertEqual([i["id"] for i in self.call("get", mine).json()], [row.pk])
        self.assertEqual(self.call("get", mine, "bob").json(), [])
        self.assertEqual(self.call("post", cancel, "bob").status_code, 403)
        row.refresh_from_db()
        self.assertEqual(row.status, "ACTIVE")
        self.assertEqual(self.call("post", cancel).json()["status"], "CANCELLED")

    @stage("A6")
    def test_capacity(self):
        path = "/api/bookings/"
        self.assertEqual(self.call("post", path, data=self.data).json()["status"], "ACTIVE")
        data = dict(self.data, room_id=self.big.pk)
        self.assertEqual(self.call("post", path, data=data).json()["status"], "PENDING")
        self.assertEqual(self.call("post", path, "bob", data).status_code, 409)

    @stage("B5")
    def test_approval(self):
        row = self.row()
        row.status = "PENDING"
        row.save()
        path = f"/api/approvals/{row.pk}/decision/"
        payload = {"decision": "reject"}
        self.assertEqual(self.call("post", path, "alice", payload).status_code, 403)
        self.assertIn(row.pk, [r["id"] for r in self.call("get", "/api/approvals/", "admin").json()])
        first = self.call("post", path, "admin", payload).json()
        self.assertEqual(first["status"], "REJECTED")
        second = self.call("post", path, "admin", payload).json()
        self.assertEqual(first["decided_at"], second["decided_at"])
        self.assertEqual(self.call("post", path, "admin", {"decision":"approve"}).status_code, 409)


from django.test import TransactionTestCase  # noqa: E402


class ConcurrentReservation(TransactionTestCase):
    @stage("A2")
    def test_postgres_allows_only_one_booking(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from django.db import connection, connections
        from django.test import Client
        self.assertEqual(connection.vendor, "postgresql")
        room = Room.objects.create(name="race", capacity=101)
        for name in ("one", "two"):
            get_user_model().objects.create_user(name, password="race")
        both = Barrier(2, timeout=15)

        def create(name):
            try:
                both.wait()
                auth = base64.b64encode((name+":race").encode()).decode()
                return Client().post("/api/bookings/",
                    {"room_id":room.pk, "start":"2031-04-01T08:00:00Z", "end":"2031-04-01T09:00:00Z"},
                    content_type="application/json", HTTP_AUTHORIZATION="Basic "+auth).status_code
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, ["one","two"], timeout=30))
        self.assertEqual(sorted(results), [201,409])
        self.assertEqual(Booking.objects.filter(room=room).count(), 1)
