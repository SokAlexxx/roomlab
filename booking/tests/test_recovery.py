"""Регресія KAN-16 / B6: готовність (/health/) при недоступній БД і після відновлення.

Збій імітується справжнім відмовленим з'єднанням: параметри підключення Django
тимчасово вказують на закритий порт, тож psycopg кидає OperationalError, а не мок.
Після відновлення параметрів те саме з'єднання перепідключається.
"""
from django.db import connection
from django.test import TransactionTestCase


class HealthOutageRecovery(TransactionTestCase):
    def setUp(self):
        self.saved = {k: connection.settings_dict.get(k) for k in ("HOST", "PORT")}
        self.addCleanup(self.restore)

    def restore(self):
        connection.settings_dict.update(self.saved)
        connection.close()

    def break_db(self):
        connection.close()
        connection.settings_dict.update(HOST="127.0.0.1", PORT="1")

    def test_503_during_outage_and_200_after_recovery(self):
        before = self.client.get("/health/")
        self.assertEqual(before.status_code, 200)
        self.assertEqual(before.json()["status"], 200)

        self.break_db()
        for _ in range(3):
            down = self.client.get("/health/")
            self.assertEqual(down.status_code, 503)
            self.assertEqual(down.json()["status"], 503)

        self.restore()
        for _ in range(2):
            up = self.client.get("/health/")
            self.assertEqual(up.status_code, 200)
            self.assertEqual(up.json()["status"], 200)
