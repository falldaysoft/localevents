from django.apps import AppConfig


class SponsorsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "sponsors"
    verbose_name = "Sponsorships"

    def ready(self):
        from . import signals  # noqa: F401
