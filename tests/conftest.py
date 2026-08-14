import pytest


@pytest.fixture(autouse=True)
def run_tasks_immediately(settings):
    """Run enqueued tasks inline.

    Tests should not need a worker process running. The immediate backend
    executes on enqueue, so a test can assert on the result of a task the code
    under test fired off.
    """
    settings.TASKS = {
        "default": {"BACKEND": "django_tasks.backends.immediate.ImmediateBackend"}
    }


@pytest.fixture(autouse=True)
def no_real_credentials(settings):
    """Keep the developer's real API key out of the test run.

    settings.py loads .env for local development, which means a machine with a
    working OPENROUTER_API_KEY would let any test that reaches the enrichment
    code make a real, billable network call — and hang for a minute or two
    doing it. Blanking the key makes that fail fast and loudly instead.
    """
    settings.OPENROUTER_API_KEY = ""


@pytest.fixture(autouse=True)
def plain_static_storage(settings):
    """Don't require a collectstatic run to render a template.

    pytest-django forces DEBUG off, which would otherwise select the manifest
    backend and blow up on the first `{% static %}` for a file shipped by a
    dependency (allauth's passkey JS, for one).
    """
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"
        },
    }


@pytest.fixture(autouse=True)
def unclaimed_site():
    """Forget whether a superuser was seen in an earlier test.

    `accounts.claim` latches its answer for the life of the process, which is
    right for a running site and wrong for a test run that creates and rolls
    back superusers all day. Without this, the first test to claim a site makes
    every later test believe the site is claimed.
    """
    from accounts.claim import reset_claim_cache

    reset_claim_cache()
    yield
    reset_claim_cache()


@pytest.fixture
def moderator(db, django_user_model):
    from django.contrib.auth.models import Group

    user = django_user_model.objects.create_user(
        username="mod", email="mod@example.com", password="pw"
    )
    group, _ = Group.objects.get_or_create(name="Moderators")
    user.groups.add(group)
    return user


@pytest.fixture
def submitter(db, django_user_model):
    return django_user_model.objects.create_user(
        username="submitter", email="submitter@example.com", password="pw"
    )


def edit_post(event, rows=None, initial=None, total=None, **overrides):
    """A complete POST to the moderator's edit screen.

    The listing's fields and its dates are one form on one screen, and a
    ModelForm POST is all-or-nothing — a field left out of the data is a field
    cleared. So every test that saves has to send the lot, and building that in
    one place is what stops a new field breaking twenty tests at once.

    Dates default to the event's current occurrences, restated: most tests are
    changing something else and just need them to survive the round trip.

    A plain function rather than a fixture because the tests that want it want
    it inside an existing call, not as another argument on thirty signatures.
    """
    from django.utils import timezone

    occurrences = list(event.occurrences.order_by("start"))
    if rows is None:
        rows = [
            {"start": timezone.localtime(o.start).strftime("%Y-%m-%dT%H:%M")}
            for o in occurrences
        ]
    if initial is None:
        initial = len(occurrences)

    venue, organizer = event.venue, event.organizer
    data = {
        "title": event.title,
        "summary": event.summary,
        "description": event.description,
        "venue_name": venue.name if venue else "",
        "venue_address": venue.address if venue else "",
        "venue_city": venue.city if venue else "",
        "organizer_name": organizer.name if organizer else "",
        "listing_type": event.listing_type,
        "prominence": event.prominence,
        "status": event.status,
        "price_note": event.price_note,
        "accessibility_notes": event.accessibility_notes,
        "source_url": event.source_url,
        "ticket_url": event.ticket_url,
        "image_url": event.image_url,
        "dates-TOTAL_FORMS": str(total if total is not None else len(rows)),
        "dates-INITIAL_FORMS": str(initial),
        "dates-MIN_NUM_FORMS": "0",
        "dates-MAX_NUM_FORMS": "60",
    }
    for index, row in enumerate(rows):
        for field, value in row.items():
            data[f"dates-{index}-{field}"] = value
    data.update(overrides)
    return data
