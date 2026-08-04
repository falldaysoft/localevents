"""Raw HTML in <head>, set from the admin.

Every instance eventually needs a tag in the head that the product cannot know
about: a search-console verification token, an analytics snippet, whatever the
council asks for this month. Those belong to the instance, not to the codebase,
which is the same reasoning that keeps a town's name out of the templates.

The half that is easy to get wrong is not the field, it is that a pasted
snippet has to survive the Content-Security-Policy. A blocked script and a
never-pasted script look identical from the admin.
"""

import pytest

from core.models import SiteConfig

CSP_HEADER = "Content-Security-Policy"


@pytest.fixture
def config(db):
    return SiteConfig.load()


@pytest.mark.django_db
def test_nothing_is_injected_by_default(client):
    """The product ships neutral: no instance has spoken yet."""
    body = client.get("/").content.decode()
    head = body[: body.index("</head>")]

    assert "SITE_HEAD_HTML" not in body
    assert head.count("<script") == 3, "the base template's own three, and no more"


@pytest.mark.django_db
def test_a_meta_tag_reaches_the_head_unescaped(client, config):
    """The plainest case, and the most common one."""
    config.head_html = '<meta name="google-site-verification" content="abc123">'
    config.save()

    body = client.get("/").content.decode()
    head = body[: body.index("</head>")]

    assert '<meta name="google-site-verification" content="abc123">' in head
    assert "&lt;meta" not in body, "the field is raw HTML, not text"


@pytest.mark.django_db
def test_an_inline_script_is_given_the_nonce(client, config):
    """Otherwise the CSP drops it and the admin has no way to find out why.

    Nobody pasting a snippet from an analytics provider knows this site's
    scripts are nonce-based, and no provider's copy-paste block carries one.
    """
    config.head_html = "<script>window.dataLayer = [];</script>"
    config.save()

    response = client.get("/")
    body = response.content.decode()

    nonce = response.headers[CSP_HEADER].split("'nonce-")[1].split("'")[0]
    assert f'<script nonce="{nonce}">window.dataLayer = [];</script>' in body


@pytest.mark.django_db
def test_an_existing_nonce_is_left_alone(client, config):
    config.head_html = '<script nonce="mine">1;</script>'
    config.save()

    body = client.get("/").content.decode()
    head = body[: body.index("</head>")]

    assert '<script nonce="mine">1;</script>' in head
    assert head.count("nonce=") == 1, "a second nonce was stamped on top"


@pytest.mark.django_db
def test_listed_hosts_are_allowed_to_load_and_to_report(client, config):
    """An analytics script that cannot post its events is as broken as one
    that cannot load, so the host goes into connect-src as well."""
    config.head_html = '<script defer src="https://plausible.example/js/x.js"></script>'
    config.script_hosts = "https://plausible.example\n"
    config.save()

    policy = client.get("/").headers[CSP_HEADER]
    directives = {
        part.strip().split(" ")[0]: part.strip()
        for part in policy.split(";")
        if part.strip()
    }

    assert "https://plausible.example" in directives["script-src"]
    assert "https://plausible.example" in directives["connect-src"]
    assert "'self'" in directives["script-src"], "the default policy is widened, not replaced"


@pytest.mark.django_db
def test_an_unlisted_host_stays_blocked(client, config):
    """The field is an allowlist, not a formality — pasting a script tag does
    not by itself grant its origin permission to run."""
    config.head_html = '<script src="https://tracker.example/x.js"></script>'
    config.save()

    policy = client.get("/").headers[CSP_HEADER]

    assert "tracker.example" not in policy


@pytest.mark.django_db
def test_blank_hosts_leave_the_policy_exactly_as_shipped(client, config):
    config.head_html = '<meta name="x" content="y">'
    config.script_hosts = "\n  \n"
    config.save()

    policy = client.get("/").headers[CSP_HEADER]

    assert "script-src 'self'" in policy
    assert policy.count("script-src") == 1


@pytest.mark.django_db
def test_only_a_superuser_may_edit_the_head(rf, moderator, django_user_model):
    """A moderator with admin access could otherwise put script on every page.

    Nobody is set up that way today — moderation has its own pages and its
    group is not staff — but the difference between editing prose and editing
    what runs in every visitor's browser should not depend on that staying
    true.
    """
    from django.contrib.admin.sites import AdminSite

    from core.admin import SiteConfigAdmin

    admin_view = SiteConfigAdmin(SiteConfig, AdminSite())
    request = rf.get("/admin/core/siteconfig/1/change/")

    request.user = moderator
    readonly = admin_view.get_readonly_fields(request)
    assert "head_html" in readonly
    assert "script_hosts" in readonly

    request.user = django_user_model.objects.create_superuser(
        username="root", email="root@example.com", password="pw-12345678"
    )
    assert admin_view.get_readonly_fields(request) == ()


@pytest.mark.django_db
def test_the_page_still_renders_when_the_config_is_unreachable(client, monkeypatch):
    """A broken head must not be a broken site.

    This runs on every response, including the ones served while the database
    is unhappy. Failing open costs an analytics tag; failing closed costs the
    page.
    """
    from core import context_processors, middleware

    def unreachable(request):
        raise RuntimeError("database is down")

    monkeypatch.setattr(context_processors, "site_config_for", unreachable)
    monkeypatch.setattr(middleware, "site_config_for", unreachable)

    response = client.get("/")

    assert response.status_code == 200
    assert "script-src 'self'" in response.headers[CSP_HEADER]
