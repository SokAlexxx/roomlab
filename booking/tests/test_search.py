import base64
from datetime import datetime, timedelta, timezone

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase

from booking.models import Booking, Room
from booking.rooms import parse_instant

PATH = "/api/rooms/available/"
UTC = timezone.utc


def at(hour, minute=0):
    return datetime(2030, 1, 1, hour, minute, tzinfo=UTC)


class AvailabilitySearch(TestCase):
    def setUp(self):
        self.r100 = Room.objects.create(name="R100", capacity=100)
        self.r101 = Room.objects.create(name="R101", capacity=101)
        self.alice = get_user_model().objects.create_user("alice", password="test")
        self.auth = "Basic " + base64.b64encode(b"alice:test").decode()
        # Existing booking in R100: [10:00, 11:00)
        self.booking = Booking.objects.create(owner=self.alice, room=self.r100,
                                              start=at(10), end=at(11), status="ACTIVE")

    def search(self, start, end, **extra):
        params = {}
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        return self.client.get(PATH, params, HTTP_AUTHORIZATION=self.auth, **extra)

    def ids(self, start, end):
        response = self.search(start, end)
        self.assertEqual(response.status_code, 200, response.content)
        return [r["id"] for r in response.json()]

    def test_runs_on_postgresql(self):
        self.assertEqual(connection.vendor, "postgresql")

    def test_requires_authentication(self):
        response = self.client.get(PATH, {"start": "2030-01-01T10:00:00Z", "end": "2030-01-01T11:00:00Z"})
        self.assertEqual(response.status_code, 401)

    def test_only_get_allowed(self):
        response = self.client.post(PATH, {}, content_type="application/json", HTTP_AUTHORIZATION=self.auth)
        self.assertEqual(response.status_code, 405)

    def test_response_shape_and_order(self):
        response = self.search("2030-01-02T10:00:00Z", "2030-01-02T11:00:00Z")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [
            {"id": self.r100.pk, "name": "R100", "capacity": 100},
            {"id": self.r101.pk, "name": "R101", "capacity": 101},
        ])

    def test_same_room_exact_slot_is_busy_other_room_free(self):
        self.assertEqual(self.ids("2030-01-01T10:00:00Z", "2030-01-01T11:00:00Z"), [self.r101.pk])

    def test_booking_in_other_room_does_not_block(self):
        Booking.objects.create(owner=self.alice, room=self.r101, start=at(14), end=at(15))
        self.assertEqual(self.ids("2030-01-01T14:00:00Z", "2030-01-01T15:00:00Z"), [self.r100.pk])

    def test_partial_overlaps(self):
        cases = [
            ("2030-01-01T09:30:00Z", "2030-01-01T10:30:00Z"),  # overlaps the start
            ("2030-01-01T10:30:00Z", "2030-01-01T11:30:00Z"),  # overlaps the end
            ("2030-01-01T10:15:00Z", "2030-01-01T10:45:00Z"),  # inside
            ("2030-01-01T09:00:00Z", "2030-01-01T12:00:00Z"),  # contains
            ("2030-01-01T10:59:59Z", "2030-01-01T11:00:01Z"),  # one second
        ]
        for start, end in cases:
            with self.subTest(start=start, end=end):
                self.assertEqual(self.ids(start, end), [self.r101.pk])

    def test_adjacent_before_is_free(self):
        self.assertEqual(self.ids("2030-01-01T09:00:00Z", "2030-01-01T10:00:00Z"),
                         [self.r100.pk, self.r101.pk])

    def test_adjacent_after_is_free(self):
        self.assertEqual(self.ids("2030-01-01T11:00:00Z", "2030-01-01T12:00:00Z"),
                         [self.r100.pk, self.r101.pk])

    def test_other_offset_same_instant_is_busy(self):
        # 12:30+02:00 == 10:30Z; "+" must be percent-encoded in a query string.
        self.assertEqual(self.ids("2030-01-01T12:30:00+02:00", "2030-01-01T13:30:00+02:00"), [self.r101.pk])
        # 11:00+01:00 == 10:00Z, so [09:00Z, 10:00Z) is adjacent and free.
        self.assertEqual(self.ids("2030-01-01T09:00:00Z", "2030-01-01T11:00:00+01:00"),
                         [self.r100.pk, self.r101.pk])

    def test_cancelled_and_rejected_do_not_block(self):
        for status in ("CANCELLED", "REJECTED"):
            with self.subTest(status=status):
                self.booking.status = status
                self.booking.save()
                self.assertEqual(self.ids("2030-01-01T10:00:00Z", "2030-01-01T11:00:00Z"),
                                 [self.r100.pk, self.r101.pk])

    def test_pending_blocks(self):
        self.booking.status = "PENDING"
        self.booking.save()
        self.assertEqual(self.ids("2030-01-01T10:00:00Z", "2030-01-01T11:00:00Z"), [self.r101.pk])

    def test_cancelled_plus_active_in_same_room_still_blocks(self):
        Booking.objects.create(owner=self.alice, room=self.r100, start=at(10), end=at(11), status="CANCELLED")
        self.assertEqual(self.ids("2030-01-01T10:00:00Z", "2030-01-01T11:00:00Z"), [self.r101.pk])

    def test_invalid_input(self):
        cases = [
            (None, "2030-01-01T11:00:00Z"),                       # missing start
            ("2030-01-01T10:00:00Z", None),                       # missing end
            ("", ""),                                             # empty
            ("2030-01-01T10:00:00", "2030-01-01T11:00:00Z"),      # naive start
            ("2030-01-01T10:00:00Z", "2030-01-01T11:00:00"),      # naive end
            ("2030-01-01", "2030-01-02"),                         # date only (naive)
            ("tomorrow", "2030-01-01T11:00:00Z"),                 # garbage
            ("2030-13-01T10:00:00Z", "2030-13-01T11:00:00Z"),     # impossible month
            ("2030-01-01T11:00:00Z", "2030-01-01T11:00:00Z"),     # start == end
            ("2030-01-01T12:00:00Z", "2030-01-01T11:00:00Z"),     # start > end
            ("2030-01-01T12:00:00+02:00", "2030-01-01T10:00:00Z"),  # equal instants, other offsets
            ("2030-01-01T10:00:00 02:00", "2030-01-01T11:00:00Z"),  # unencoded "+"
            (" 2030-01-01T10:00:00Z", "2030-01-01T11:00:00Z"),    # surrounding whitespace
        ]
        for start, end in cases:
            with self.subTest(start=start, end=end):
                response = self.search(start, end)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()["code"], "invalid_input")


class ParseInstant(TestCase):
    def test_accepts_explicit_offsets(self):
        self.assertEqual(parse_instant("2030-01-01T10:00:00Z"), at(10))
        self.assertEqual(parse_instant("2030-01-01T10:00:00+00:00"), at(10))
        self.assertEqual(parse_instant("2030-01-01T12:00:00+02:00"), at(10))
        self.assertEqual(parse_instant("2030-01-01T05:30:00-04:30"), at(10))
        self.assertEqual(parse_instant("2030-01-01T10:00:00.5Z"), at(10) + timedelta(microseconds=500000))

    def test_rejects_naive_and_non_strings(self):
        for value in (None, 123, "", "2030-01-01T10:00:00", "2030-01-01"):
            with self.subTest(value=value):
                self.assertIsNone(parse_instant(value))
