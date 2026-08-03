"""Drop online events entirely.

An event happening "anywhere" has no local connection that a moderator can
check, which makes it the cheapest possible way to spam a community listing.
Removing the fields rather than hiding the filter means the site cannot
represent such a listing at all — a stronger guarantee than moderating one.

Venue stays nullable for the legitimate case of a location still to be
confirmed.
"""

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('events', '0002_seed_categories'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='event',
            name='is_online',
        ),
        migrations.RemoveField(
            model_name='event',
            name='online_url',
        ),
        migrations.AlterField(
            model_name='event',
            name='venue',
            field=models.ForeignKey(blank=True, help_text='Leave empty only if the location is still to be confirmed.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='events', to='events.venue'),
        ),
    ]
