# Written manually for KAN-8 / A5: schema v1 -> v2 for the approval flow.
# Expand-only: three nullable audit columns, two audit check constraints, and the
# overlap exclusion constraint widened from ACTIVE to ACTIVE/PENDING (same name, so
# booking.rooms.is_overlap_violation keeps mapping it to 409). No data is rewritten:
# legacy rows keep id, owner, room, interval and status; their audit columns are NULL.
# Remove + Add run in one transaction (PostgreSQL DDL is transactional), so there is no
# moment without overlap protection. See docs/migration.md for rollback limits.

import booking.models
import django.contrib.postgres.constraints
import django.contrib.postgres.fields.ranges
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def overlap_active_or_pending():
    return django.contrib.postgres.constraints.ExclusionConstraint(
        condition=models.Q(("status__in", ["ACTIVE", "PENDING"])),
        expressions=[
            ("room", "="),
            (
                booking.models.TsTzRange(
                    "start",
                    "end",
                    django.contrib.postgres.fields.ranges.RangeBoundary(),
                ),
                "&&",
            ),
        ],
        name="booking_no_overlap_active",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0002_booking_no_overlap_active"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="decided_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="booking_decisions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="decided_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="booking",
            name="decision",
            field=models.CharField(
                blank=True,
                choices=[("approve", "Approve"), ("reject", "Reject")],
                max_length=8,
                null=True,
            ),
        ),
        migrations.AddConstraint(
            model_name="booking",
            constraint=models.CheckConstraint(
                condition=models.Q(("decision__isnull", True), ("decision__in", ["approve", "reject"]), _connector="OR"),
                name="booking_decision_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="booking",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("decided_at__isnull", True), ("decided_by__isnull", True), ("decision__isnull", True)),
                    models.Q(("decided_at__isnull", False), ("decided_by__isnull", False), ("decision__isnull", False)),
                    _connector="OR",
                ),
                name="booking_decision_audit_complete",
            ),
        ),
        migrations.RemoveConstraint(
            model_name="booking",
            name="booking_no_overlap_active",
        ),
        migrations.AddConstraint(
            model_name="booking",
            constraint=overlap_active_or_pending(),
        ),
    ]
