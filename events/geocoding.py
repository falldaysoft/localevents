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


WHOLE_WORLD = (-90.0, -180.0, 90.0, 180.0)


def region_params():
    """The instance's bounds as a Nominatim `viewbox` — a preference, not a fence.

    Without `bounded=1` the box only ranks results inside it above results
    outside, which is what a local site wants: "Paris" should mean the one
    down the road, while a hall just over the county line must still resolve
    so a moderator can decide whether it belongs. The default whole-world box
    says nothing, so it is not sent. Nominatim wants the corners as lng,lat.
    """
    min_lat, min_lng, max_lat, max_lng = settings.MAP_BBOX
    if (min_lat, min_lng, max_lat, max_lng) == WHOLE_WORLD:
        return {}
    return {"viewbox": f"{min_lng},{min_lat},{max_lng},{max_lat}"}


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
            params={"q": query, "format": "jsonv2", "limit": 1, **region_params()},
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

    # Walk down to less specific queries until one lands inside the region.
    # A rung that matches somewhere else entirely does not end the walk: the
    # address-only rung for "84 Highland Drive, Paris" found a street of that
    # name in Paris, Michigan, and the name-only rung below it — which resolves
    # to the right pond in the right Paris — never ran, so the event was
    # published with a marker two provinces away. An out-of-region hit is
    # remembered and kept only if no lower rung does better; it is a flag for
    # a moderator, not a wrong answer, because plenty of communities care
    # about something just over the county line. Each extra rung costs a
    # throttled second, but only on a miss or a doubtful hit, and a venue
    # geocodes once and caches forever.
    result = None
    out_of_region = None
    for attempt, query in enumerate(queries):
        try:
            found = lookup(query)
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

        if found is None:
            continue
        if Venue.coordinates_in_region(*found):
            result = found
            if attempt:
                logger.info(
                    "geocoded venue %s with a fallback query: %r", venue_id, query
                )
            break
        if out_of_region is None:
            out_of_region = found

    if result is None and out_of_region is None:
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

    if result is not None:
        venue.geocode_status = Venue.GeocodeStatus.OK
    else:
        result = out_of_region
        venue.geocode_status = Venue.GeocodeStatus.OUT_OF_REGION

    venue.latitude, venue.longitude = result
    venue.geocode_error = ""

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
