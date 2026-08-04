import re

from django.conf import settings
from django.middleware.csp import get_nonce
from django.utils.safestring import mark_safe

from .models import site_config_for

# A <script> without the request's nonce is dead on arrival under this site's
# CSP, and someone pasting a snippet from an analytics provider has no reason
# to know that. Stamp the nonce on any script tag that does not carry one.
_UNNONCED_SCRIPT = re.compile(r"<script\b(?![^>]*\bnonce=)", re.IGNORECASE)


def site_head(request):
    """Whatever an administrator has asked to appear inside <head>.

    Marked safe deliberately: the field exists to inject raw HTML, and the
    admin restricts it to superusers, who can already change anything. See
    core.middleware.SiteHeadCSPMiddleware for the other half — the CSP has to
    allow what this loads or the tag is inert.
    """
    try:
        head_html = site_config_for(request).head_html
    except Exception:
        # Rendering an error page must not depend on the database being up.
        return {"SITE_HEAD_HTML": ""}

    if not head_html.strip():
        return {"SITE_HEAD_HTML": ""}

    # `is not None`, not a truth test: the nonce is lazy and reads as False
    # until something forces it, so `if nonce:` silently stamps nothing on the
    # very pages that need it. Interpolating it below is what generates it.
    # The replacement is a callable so a nonce is never read as a regex escape.
    nonce = get_nonce(request)
    if nonce is not None:
        head_html = _UNNONCED_SCRIPT.sub(
            lambda _: f'<script nonce="{nonce}"', head_html
        )
    return {"SITE_HEAD_HTML": mark_safe(head_html)}  # noqa: S308 — see docstring


def site(request):
    """Expose instance identity to every template.

    Templates must never hardcode a place name — they read these instead, which
    is what lets another community deploy this unchanged.
    """
    return {
        "SITE_NAME": settings.SITE_NAME,
        "SITE_TAGLINE": settings.SITE_TAGLINE,
        "CONTACT_EMAIL": settings.CONTACT_EMAIL,
        "TILE_URL": settings.TILE_URL,
        "TILE_ATTRIBUTION": settings.TILE_ATTRIBUTION,
    }
