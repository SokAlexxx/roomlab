# Written manually for KAN-5 / A2: btree_gist must exist before the GiST
# exclusion constraint, because "room_id WITH =" needs a btree_gist operator class.

import booking.models
import django.contrib.postgres.constraints
import django.contrib.postgres.fields.ranges
from django.contrib.postgres.operations import BtreeGistExtension
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("booking", "0001_initial"),
    ]

    operations = [
        BtreeGistExtension(),
        migrations.AddConstraint(
            model_name="booking",
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(
                condition=models.Q(("status", "ACTIVE")),
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
            ),
        ),
    ]
