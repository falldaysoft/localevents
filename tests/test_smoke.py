import pytest
from django.urls import reverse


def test_healthz_does_not_touch_the_database(client):
    """No `db` fixture here on purpose: the liveness probe must not query."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.content == b"ok"


@pytest.mark.django_db
def test_index_renders(client):
    response = client.get(reverse("index"))
    assert response.status_code == 200


@pytest.mark.django_db
def test_login_page_renders(client):
    response = client.get(reverse("account_login"))
    assert response.status_code == 200


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
