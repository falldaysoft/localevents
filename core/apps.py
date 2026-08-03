from urllib.parse import urlparse

from django.apps import AppConfig
from django.db.models.signals import post_migrate


def sync_site(sender, **kwargs):
    """Point `django.contrib.sites` at this instance.

    allauth builds its outbound mail from the Site row, not from SITE_NAME, so
    a database still holding Django's default row sends "Hello from
    example.com!", signed example.com — the exact shape of message a recipient
    deletes as phishing. Nothing in Django ever updates that row after creating
    it, so the mistake is silent and permanent.

    On post_migrate rather than in a data migration because these values come
    from the environment. A data migration would bake in whatever was set the
    day it first ran and then go quietly stale the next time SITE_NAME or the
    host changed — and having been applied, it would never revisit. Migrations
    run on every deploy through the chart's pre-upgrade hook, so hanging this
    off them keeps the row honest for free.

    The domain comes from SITE_BASE_URL because that setting already has to be
    right for links in mail to work at all: one thing to get wrong, not two.
    """
    from django.conf import settings
    from django.contrib.sites.models import Site

    domain = urlparse(settings.SITE_BASE_URL).netloc or settings.SITE_BASE_URL

    Site.objects.update_or_create(
        pk=settings.SITE_ID,
        defaults={"domain": domain, "name": settings.SITE_NAME},
    )


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "core"

    def ready(self):
        # sender=self so this fires once per `migrate` rather than once per
        # installed app. `django.contrib.sites` is listed before this app, so
        # its own receiver has already created the default row by the time
        # this corrects it.
        post_migrate.connect(sync_site, sender=self)
