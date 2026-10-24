# A fourth prominence value, between Background and Listed. The values are
# integers so a descending sort reads most-prominent-first, which means
# inserting a tier renumbers the two above it: Listed 2 → 3, Featured 3 → 4.
# One UPDATE rather than a loop of save() calls — Event.save() derives slug,
# published_at and renewal_token, none of which a renumbering should touch.

from django.db import migrations, models
from django.db.models import F


def make_room_for_quiet(apps, schema_editor):
    Event = apps.get_model("events", "Event")
    Event.objects.filter(prominence__gte=2).update(prominence=F("prominence") + 1)


def close_the_gap(apps, schema_editor):
    Event = apps.get_model("events", "Event")
    # Quiet has no home in the old scheme; it was Listed-but-less, so Listed.
    Event.objects.filter(prominence=2).update(prominence=3)
    Event.objects.filter(prominence__gte=3).update(prominence=F("prominence") - 1)


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0005_eventrefresh"),
    ]

    operations = [
        migrations.RunPython(make_room_for_quiet, close_the_gap),
        migrations.AlterField(
            model_name="event",
            name="prominence",
            field=models.IntegerField(
                choices=[
                    (1, "Background — regular program"),
                    (2, "Quiet — on the front page only when it's soon"),
                    (3, "Listed — appears in the main feed"),
                    (4, "Featured — highlighted"),
                ],
                default=3,
                help_text="Where this sits in the feed. A moderator's call.",
            ),
        ),
    ]
