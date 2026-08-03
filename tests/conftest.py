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
