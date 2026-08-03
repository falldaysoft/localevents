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
