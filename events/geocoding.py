"""Turning a venue address into coordinates, politely.

Nominatim is free and needs no key, which suits a community project, but its
usage policy is strict: at most one request per second, and a contactable
User-Agent. Both are honoured here — the rate limit through a cluster-wide
lease (see GeocodeThrottle) rather than a local sleep, because the limit
applies to the application as a whole and this runs in more than one process.

Geocoding is idempotent and cached on the venue, so the steady-state request
volume is near zero. The load case is a feed import introducing many new
venues at once.
"""

import logging

import httpx
from django.conf import settings
from django.tasks import task
from django.utils import timezone

from .models import GeocodeThrottle, Venue

logger = logging.getLogger("events.geocoding")

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
REQUEST_TIMEOUT = 10.0


class GeocodeError(Exception):
    pass


def lookup(query, *, throttle=True):
    """Ask Nominatim for coordinates. Returns (lat, lng) or None.

    Raises GeocodeError on transport or protocol failure so the caller can
    distinguish "no such place" from "could not ask".
    """
    if throttle:
        GeocodeThrottle.acquire()

    try:
        response = httpx.get(
            NOMINATIM_URL,
            params={"q": query, "format": "jsonv2", "limit": 1},
            headers={"User-Agent": settings.USER_AGENT},
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
        )
        response.raise_for_status()
        results = response.json()
    except httpx.HTTPError as exc:
        raise GeocodeError(f"request failed: {exc}") from exc
    except ValueError as exc:
        raise GeocodeError(f"malformed response: {exc}") from exc

    if not results:
        return None

    first = results[0]
    try:
        return float(first["lat"]), float(first["lon"])
    except (KeyError, TypeError, ValueError) as exc:
        raise GeocodeError(f"unexpected result shape: {exc}") from exc


@task()
def geocode_venue(venue_id):
    """Geocode one venue and record the outcome.

    Never raises: a venue that cannot be located should be visible as such to a
    moderator, not retried forever by the queue.
    """
    try:
        venue = Venue.objects.get(pk=venue_id)
    except Venue.DoesNotExist:
        logger.warning("geocode_venue: venue %s no longer exists", venue_id)
        return

    if venue.geocode_status == Venue.GeocodeStatus.MANUAL:
        return

    queries = venue.geocode_queries()
    if not queries:
        venue.geocode_status = Venue.GeocodeStatus.FAILED
        venue.geocode_error = "No address to search for."
        venue.geocode_attempted_at = timezone.now()
        venue.save(
            update_fields=["geocode_status", "geocode_error", "geocode_attempted_at"]
        )
        return

    venue.geocode_attempted_at = timezone.now()

    # Walk down to less specific queries until one matches. Each rung costs a
    # throttled second, but only on a miss, and a venue geocodes once and
    # caches forever — so the cost lands on the rare failure, not the norm.
    result = None
    for attempt, query in enumerate(queries):
        try:
            result = lookup(query)
        except GeocodeError as exc:
            logger.warning("geocode failed for venue %s: %s", venue_id, exc)
            venue.geocode_status = Venue.GeocodeStatus.FAILED
            venue.geocode_error = str(exc)[:300]
            venue.save(
                update_fields=[
                    "geocode_status", "geocode_error", "geocode_attempted_at"
                ]
            )
            return

        if result is not None:
            if attempt:
                logger.info(
                    "geocoded venue %s with a fallback query: %r", venue_id, query
                )
            break

    if result is None:
        venue.geocode_status = Venue.GeocodeStatus.FAILED
        # Name the queries that missed. "No match found" sent one investigation
        # looking at the network and the User-Agent when the answer was that
        # the address and the venue name did not agree.
        venue.geocode_error = "No match found for: " + "; ".join(
            repr(q) for q in queries
        )
        venue.save(
            update_fields=["geocode_status", "geocode_error", "geocode_attempted_at"]
        )
        return

    latitude, longitude = result
    venue.latitude = latitude
    venue.longitude = longitude
    venue.geocode_error = ""

    # Outside the configured bounds is a flag, not a failure — a moderator
    # decides whether somewhere just over the line still belongs.
    if Venue.coordinates_in_region(latitude, longitude):
        venue.geocode_status = Venue.GeocodeStatus.OK
    else:
        venue.geocode_status = Venue.GeocodeStatus.OUT_OF_REGION

    venue.save(
        update_fields=[
            "latitude",
            "longitude",
            "geocode_status",
            "geocode_error",
            "geocode_attempted_at",
        ]
    )


@task()
def geocode_pending_venues(limit=50):
    """Sweep venues that still need coordinates.

    Bounded so one run cannot monopolise the worker: at one request per second
    a 50-venue batch takes under a minute, and the next scheduled run picks up
    the rest.
    """
    pending = Venue.objects.filter(
        geocode_status=Venue.GeocodeStatus.PENDING
    ).order_by("created_at")[:limit]

    for venue in pending:
        geocode_venue.enqueue(venue.pk)
