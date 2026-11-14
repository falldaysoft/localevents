from core.models import site_config_for


def weekly_email(request):
    """Whether the weekly email is switched on, for the footer and profile.

    Read through `site_config_for`, which the theme processor has already
    loaded, so this costs no query. Off when the database is unreachable,
    for the same reason `core.context_processors.site_head` swallows errors.
    """
    try:
        return {"WEEKLY_EMAIL_ON": site_config_for(request).digest_enabled}
    except Exception:
        return {"WEEKLY_EMAIL_ON": False}
