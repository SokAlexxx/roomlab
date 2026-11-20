"""v1 end-to-end flow and request telemetry (KAN-13 / B3).

The E2E scenario goes through the real URLconf, middleware and views with
the Django test client on PostgreSQL; nothing is mocked.
"""
import base64
import json
import re
from datetime import datetime, timedelta, timezone

from django.contrib.auth import get_user_model
from django.test import RequestFactory, SimpleTestCase, TestCase

from booking.models import Booking, Room
from booking.telemetry import RequestTelemetry

LOGGER = "roomlab.request"
FIELDS = {"time", "operation", "status", "elapsed_ms", "request_id"}
PASSWORD = "s3cret-Pa55"
SLOT = {"start": "2030-01-01T10:00:00Z", "end": "2030-01-01T11:00:00Z"}


def basic(name):
    return "Basic " + base64.b64encode(f"{name}:{PASSWORD}".encode()).decode()


def parse(captured):
    return [json.loads(record.getMessage()) for record in captured.records]


class Api(TestCase):
    def setUp(self):
        users = get_user_model().objects
        self.alice = users.create_user("alice", password=PASSWORD)
        self.bob = users.create_user("bob", password=PASSWORD)
        self.room = Room.objects.create(name="R100", capacity=100)

    def get(self, path, user="alice", **extra):
        return self.client.get(path, HTTP_AUTHORIZATION=basic(user), **extra)

    def post(self, path, data, user="alice", **extra):
        return self.client.post(path, data, content_type="application/json",
                                HTTP_AUTHORIZATION=basic(user), **extra)


class V1EndToEnd(Api):
    def test_create_list_foreign_cancel_cancel_create_again(self):
        booking = {"room_id": self.room.pk, **SLOT}
        with self.assertLogs(LOGGER, "INFO") as captured:
            # 1. alice creates a booking.
            created = self.post("/api/bookings/", booking)
            self.assertEqual(created.status_code, 201)
            first = created.json()
            self.assertEqual(first["status"], "ACTIVE")
            self.assertEqual(first["room_id"], self.room.pk)

            # 2. It shows up in her own list, not in bob's.
            mine = self.get("/api/bookings/mine/")
            self.assertEqual(mine.status_code, 200)
            self.assertEqual(mine.json(), [first])
            self.assertEqual(self.get("/api/bookings/mine/", "bob").json(), [])

            # 3. bob cannot cancel it; the row is untouched.
            before = Booking.objects.values().get(pk=first["id"])
            foreign = self.post(f"/api/bookings/{first['id']}/cancel/", {}, "bob")
            self.assertEqual(foreign.status_code, 403)
            self.assertEqual(foreign.json()["code"], "forbidden")
            self.assertEqual(Booking.objects.values().get(pk=first["id"]), before)

            # The slot is still taken while the booking is active.
            self.assertEqual(self.post("/api/bookings/", booking, "bob").status_code, 409)

            # 4. alice cancels; the slot is free immediately.
            cancelled = self.post(f"/api/bookings/{first['id']}/cancel/", {})
            self.assertEqual(cancelled.status_code, 200)
            self.assertEqual(cancelled.json(), {**first, "status": "CANCELLED"})
            free = self.get("/api/rooms/available/", data=SLOT)
            self.assertIn(self.room.pk, [room["id"] for room in free.json()])

            # 5. The same slot can be booked again, as a new record.
            again = self.post("/api/bookings/", booking)
            self.assertEqual(again.status_code, 201)
            second = again.json()
            self.assertNotEqual(second["id"], first["id"])
            self.assertEqual(second["status"], "ACTIVE")
            statuses = {row["id"]: row["status"] for row in self.get("/api/bookings/mine/").json()}
            self.assertEqual(statuses, {first["id"]: "CANCELLED", second["id"]: "ACTIVE"})

        lines = parse(captured)
        self.assertEqual([(line["operation"], line["status"]) for line in lines], [
            ("POST bookings-create", 201),
            ("GET bookings-mine", 200),
            ("GET bookings-mine", 200),
            ("POST bookings-cancel", 403),
            ("POST bookings-create", 409),
            ("POST bookings-cancel", 200),
            ("GET rooms-available", 200),
            ("POST bookings-create", 201),
            ("GET bookings-mine", 200),
        ])
        self.assertEqual(len({line["request_id"] for line in lines}), len(lines))


class TelemetryLogging(Api):
    def logged(self, call):
        with self.assertLogs(LOGGER, "INFO") as captured:
            response = call()
        lines = parse(captured)
        self.assertEqual(len(lines), 1)
        return response, lines[0], captured.output[0]

    def test_line_has_exact_structured_fields(self):
        response, line, _ = self.logged(lambda: self.get("/api/bookings/mine/"))
        self.assertEqual(set(line), FIELDS)
        self.assertEqual(line["operation"], "GET bookings-mine")
        self.assertEqual(line["status"], 200)
        self.assertIsInstance(line["elapsed_ms"], (int, float))
        self.assertGreaterEqual(line["elapsed_ms"], 0)
        self.assertRegex(line["request_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(response["X-Request-ID"], line["request_id"])

    def test_time_is_utc_and_current(self):
        _, line, _ = self.logged(lambda: self.get("/api/bookings/mine/"))
        logged_at = datetime.fromisoformat(line["time"])
        self.assertEqual(logged_at.utcoffset(), timedelta(0))
        self.assertLess(abs(datetime.now(timezone.utc) - logged_at), timedelta(minutes=1))

    def test_no_credentials_or_request_data_logged(self):
        data = {"room_id": self.room.pk, **SLOT}
        _, _, raw = self.logged(lambda: self.post("/api/bookings/", data))
        for secret in (PASSWORD, basic("alice"), basic("alice").split()[1], "alice",
                       "Authorization", "2030-01-01"):
            self.assertNotIn(secret, raw)

    def test_operation_uses_route_name_not_path(self):
        row = Booking.objects.create(owner=self.alice, room=self.room,
                                     start=datetime(2030, 1, 1, 10, tzinfo=timezone.utc),
                                     end=datetime(2030, 1, 1, 11, tzinfo=timezone.utc))
        _, line, raw = self.logged(lambda: self.post(f"/api/bookings/{row.pk}/cancel/", {}))
        self.assertEqual(line["operation"], "POST bookings-cancel")
        self.assertNotIn("/api/", raw)

    def test_failures_are_logged_with_status(self):
        _, line, _ = self.logged(lambda: self.client.get("/api/bookings/mine/"))
        self.assertEqual((line["operation"], line["status"]), ("GET bookings-mine", 401))
        _, line, _ = self.logged(lambda: self.get("/api/bookings/mine/", "nobody"))
        self.assertEqual(line["status"], 401)
        _, line, _ = self.logged(lambda: self.client.get("/no/such/path/?token=x"))
        self.assertEqual((line["operation"], line["status"]), ("GET unresolved", 404))

    def test_safe_client_request_id_is_reused(self):
        response, line, _ = self.logged(
            lambda: self.get("/api/bookings/mine/", HTTP_X_REQUEST_ID="trace-42.a_b"))
        self.assertEqual(line["request_id"], "trace-42.a_b")
        self.assertEqual(response["X-Request-ID"], "trace-42.a_b")

    def test_unsafe_client_request_id_is_replaced(self):
        for bad in ('x"}\n{"status":200', "a" * 65, "id with spaces", basic("alice")):
            _, line, raw = self.logged(lambda: self.get("/api/bookings/mine/", HTTP_X_REQUEST_ID=bad))
            self.assertRegex(line["request_id"], r"^[0-9a-f]{32}$")
            self.assertNotIn(bad, raw)


class TelemetryOnException(SimpleTestCase):
    def test_view_exception_is_logged_as_500_and_reraised(self):
        def explode(request):
            raise RuntimeError("boom")

        request = RequestFactory().get("/api/bookings/mine/")
        with self.assertLogs(LOGGER, "INFO") as captured, self.assertRaises(RuntimeError):
            RequestTelemetry(explode)(request)
        line = parse(captured)[0]
        self.assertEqual((line["operation"], line["status"]), ("GET bookings-mine", 500))
        self.assertTrue(re.fullmatch(r"[0-9a-f]{32}", line["request_id"]))
