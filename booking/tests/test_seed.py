from datetime import datetime, timezone
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from booking.models import Booking, Room


def seed(password="demo-one"):
    with mock.patch.dict("os.environ", {"DEMO_PASS": password}):
        call_command("seed_demo", stdout=StringIO())


class SeedDemoTests(TestCase):
    def test_creates_contract_rooms_and_users(self):
        seed()
        self.assertEqual(list(Room.objects.order_by("name").values_list("name", "capacity")),
                         [("R100", 100), ("R101", 101)])
        users = {u.username: u for u in get_user_model().objects.all()}
        self.assertEqual(set(users), {"alice", "bob", "admin"})
        self.assertEqual([n for n, u in users.items() if u.is_staff], ["admin"])
        self.assertFalse(any(u.is_superuser for u in users.values()))
        for user in users.values():
            self.assertTrue(user.check_password("demo-one"))

    def test_rerun_is_idempotent_and_preserves_ids_and_bookings(self):
        seed()
        room_ids = dict(Room.objects.values_list("name", "pk"))
        user_ids = dict(get_user_model().objects.values_list("username", "pk"))
        alice = get_user_model().objects.get(username="alice")
        booking = Booking.objects.create(
            owner=alice, room_id=room_ids["R100"],
            start=datetime(2030, 1, 1, 10, tzinfo=timezone.utc),
            end=datetime(2030, 1, 1, 11, tzinfo=timezone.utc))
        before = Booking.objects.values().get(pk=booking.pk)
        seed()
        seed()
        self.assertEqual(Room.objects.count(), 2)
        self.assertEqual(get_user_model().objects.count(), 3)
        self.assertEqual(dict(Room.objects.values_list("name", "pk")), room_ids)
        self.assertEqual(dict(get_user_model().objects.values_list("username", "pk")), user_ids)
        self.assertEqual(Booking.objects.values().get(pk=booking.pk), before)

    def test_rerun_resets_passwords_and_flags(self):
        seed("old-pass")
        User = get_user_model()
        bob = User.objects.get(username="bob")
        bob.set_password("changed")
        bob.is_staff = True
        bob.is_active = False
        bob.save()
        admin = User.objects.get(username="admin")
        admin.is_staff = False
        admin.save()
        Room.objects.filter(name="R101").update(capacity=5)
        seed("new-pass")
        for user in User.objects.all():
            self.assertTrue(user.check_password("new-pass"), user.username)
            self.assertFalse(user.check_password("old-pass"))
        bob.refresh_from_db()
        admin.refresh_from_db()
        self.assertFalse(bob.is_staff)
        self.assertTrue(bob.is_active)
        self.assertTrue(admin.is_staff)
        self.assertEqual(Room.objects.get(name="R101").capacity, 101)

    def test_keeps_preexisting_rows(self):
        room = Room.objects.create(name="R100", capacity=100)
        user = get_user_model().objects.create_user("alice", password="x")
        seed()
        self.assertEqual(Room.objects.get(name="R100").pk, room.pk)
        self.assertEqual(get_user_model().objects.get(username="alice").pk, user.pk)

    def test_requires_demo_pass(self):
        with mock.patch.dict("os.environ", {"DEMO_PASS": ""}):
            with self.assertRaises(CommandError):
                call_command("seed_demo", stdout=StringIO())
        self.assertFalse(Room.objects.exists())
        self.assertFalse(get_user_model().objects.exists())
