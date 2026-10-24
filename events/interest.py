"""Who is pressing the star.

A signed-in reader is their `User` row. An anonymous reader is a cookie: a
random id, signed so the value cannot be minted or guessed, set on the *first*
click rather than on page view so browsing leaves nothing behind and the
cookie is the direct result of something the reader asked for.

The row stores an HMAC of that id, never the id itself, so a database dump
hands out no valid cookies. The IP address is deliberately not part of the
fingerprint, although the field's first help text said it would be — a phone
changes address daily, and a mark the site forgets by tomorrow is not the
remembering it was asked for. The address goes to the rate limiter instead,
which is the only place it is any use.

Bot protection here is sized to *accidental* traffic. The real gate is that
the toggle is a POST behind CSRF: a crawler, a link prefetcher or a chat
unfurler GETs, holds no CSRF cookie from a prior render, and never reaches the
view. The per-address cap below bounds the one case that gets past that — a
scanner that renders pages, submits every form it finds and discards cookies
between hits, which would otherwise mint one anonymous row per hit.
"""

import secrets

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.utils.crypto import salted_hmac

COOKIE_NAME = "visitor"
COOKIE_MAX_AGE = 60 * 60 * 24 * 365
_SALT = "events.interest.visitor"


def visitor_id(request):
    """The anonymous id this browser carries, or None.

    A cookie whose signature fails is treated as absent rather than rejected:
    the right response to a tampered or stale cookie is a fresh identity, not
    an error on a star button.
    """
    raw = request.COOKIES.get(COOKIE_NAME)
    if not raw:
        return None
    try:
        return signing.loads(raw, salt=_SALT)
    except signing.BadSignature:
        return None


def new_visitor_id():
    return secrets.token_urlsafe(16)


def set_visitor_cookie(response, vid):
    response.set_cookie(
        COOKIE_NAME,
        signing.dumps(vid, salt=_SALT),
        max_age=COOKIE_MAX_AGE,
        httponly=True,
        secure=not settings.DEBUG,
        samesite="Lax",
    )


def fingerprint_for(vid):
    """What the Interest row stores for an anonymous visitor."""
    return salted_hmac(_SALT, vid).hexdigest()


def fingerprint(request):
    """The fingerprint for this request's existing cookie, or None."""
    vid = visitor_id(request)
    return fingerprint_for(vid) if vid else None


def interested_event_ids(request):
    """Every event this viewer has marked, in one query.

    Listings test membership against the set rather than annotating each card
    with an `Exists`; the front page renders up to a few hundred cards and the
    viewer has marked a handful.
    """
    from events.models import Interest

    if request.user.is_authenticated:
        rows = Interest.objects.filter(user=request.user)
    else:
        fp = fingerprint(request)
        if fp is None:
            return set()
        rows = Interest.objects.filter(user__isnull=True, fingerprint=fp)
    return set(rows.values_list("event_id", flat=True))


def client_ip(request):
    """The address the rate limit keys on.

    Behind the instance's Traefik the app sees the proxy's address in
    REMOTE_ADDR and the reader's in X-Forwarded-For; Traefik overwrites that
    header rather than appending to what a client sent, so the last entry is
    the proxy's view of the client. Taking it from a header is spoofable in
    principle, which is acceptable for a nuisance limiter that is not a
    security control — the worst a spoofer achieves is escaping a cap that
    exists to catch scanners that do not spoof anything.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.rsplit(",", 1)[-1].strip()
    return request.META.get("REMOTE_ADDR", "")


def within_rate_limit(request):
    """Allow this toggle, and count it.

    Counted on the default cache. With no CACHES configured that is local
    memory, per process and lost on restart — approximate, and enough: the
    threat is a scanner hammering one address, not a distributed campaign, and
    the signal it protects cannot move an event between tiers anyway.
    """
    limit = settings.INTEREST_TOGGLES_PER_HOUR
    key = f"interest-toggles:{client_ip(request)}"
    cache.add(key, 0, timeout=60 * 60)
    try:
        count = cache.incr(key)
    except ValueError:
        # The key expired between add and incr; start the window again.
        cache.set(key, 1, timeout=60 * 60)
        count = 1
    return count <= limit
