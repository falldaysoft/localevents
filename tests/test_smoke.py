import pytest


@pytest.mark.django_db
def test_healthz_does_not_touch_the_database(client, django_assert_num_queries):
    """The liveness probe must not query. A database that hangs rather than
    refusing would otherwise block the probe, and Kubernetes would restart a
    container whose only problem was upstream.

    This counts queries instead of the older trick of omitting the `db`
    fixture. That version passed while the property was already broken:
    SiteHeadCSPMiddleware did query, and its blanket `except Exception`
    swallowed the RuntimeError that pytest-django's blocker raises. A test
    whose failure mode is silently caught by the code under test is no test.
    """
    with django_assert_num_queries(0):
        response = client.get("/healthz")
    assert response.status_code == 200
    assert response.content == b"ok"


@pytest.mark.django_db
def test_singletons_load():
    from core.models import AIConfig, SiteConfig

    assert SiteConfig.load().pk == 1
    assert AIConfig.load().pk == 1
    # load() is idempotent — it must not accumulate rows.
    SiteConfig.load()
    assert SiteConfig.objects.count() == 1


@pytest.mark.django_db
def test_user_email_is_unique(django_user_model):
    from django.db import IntegrityError

    django_user_model.objects.create_user(
        username="a", email="dup@example.com", password="pw"
    )
    with pytest.raises(IntegrityError):
        django_user_model.objects.create_user(
            username="b", email="dup@example.com", password="pw"
        )
