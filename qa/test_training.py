from django.test import TestCase


class Training(TestCase):
    def test_health_status(self):
        self.assertEqual(self.client.get("/health/").status_code, 200)
