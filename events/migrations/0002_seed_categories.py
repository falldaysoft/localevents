"""Seed the browse vocabulary.

Deliberately short and general. Filters are only useful when a category
reliably means the same thing, and a long list stops being a filter and starts
being a second navigation problem. These are also intentionally place-neutral —
an instance can rename or retire them in the admin, which is the point.

Existing rows are left alone so an instance's edits survive a redeploy.
"""

from django.db import migrations

CATEGORIES = [
    ("Music", "music", "🎵", "Concerts, gigs, open mics", 10),
    ("Arts & Theatre", "arts-theatre", "🎭", "Shows, galleries, exhibitions", 20),
    ("Family", "family", "🧒", "Suitable for children and caregivers", 30),
    ("Food & Drink", "food-drink", "🍽️", "Dinners, tastings, food events", 40),
    ("Markets", "markets", "🧺", "Farmers markets, craft fairs, sales", 50),
    ("Sports & Outdoors", "sports-outdoors", "⚽", "Games, runs, hikes, rides", 60),
    ("Learning", "learning", "📚", "Talks, classes, workshops", 70),
    ("Community", "community", "🤝", "Meetings, fundraisers, civic events", 80),
    ("Volunteering", "volunteering", "🧤", "Ways to pitch in", 90),
    ("Nightlife", "nightlife", "🌙", "Later-evening events", 100),
    ("Seasonal", "seasonal", "🎪", "Holidays, festivals, annual traditions", 110),
]


def seed(apps, schema_editor):
    Category = apps.get_model("events", "Category")
    for name, slug, emoji, description, sort_order in CATEGORIES:
        Category.objects.get_or_create(
            slug=slug,
            defaults={
                "name": name,
                "emoji": emoji,
                "description": description,
                "sort_order": sort_order,
                "is_active": True,
            },
        )


def unseed(apps, schema_editor):
    """Only remove categories that are still unused.

    Reversing a migration should not delete a moderator's curation or orphan
    published events.
    """
    Category = apps.get_model("events", "Category")
    slugs = [c[1] for c in CATEGORIES]
    Category.objects.filter(slug__in=slugs, events__isnull=True).delete()


class Migration(migrations.Migration):
    dependencies = [("events", "0001_initial")]

    operations = [migrations.RunPython(seed, unseed)]
