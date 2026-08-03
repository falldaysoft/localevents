from django.conf import settings


def site(request):
    """Expose instance identity to every template.

    Templates must never hardcode a place name — they read these instead, which
    is what lets another community deploy this unchanged.
    """
    return {
        "SITE_NAME": settings.SITE_NAME,
        "SITE_TAGLINE": settings.SITE_TAGLINE,
        "CONTACT_EMAIL": settings.CONTACT_EMAIL,
    }
