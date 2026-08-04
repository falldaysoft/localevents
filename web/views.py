import json

from django.conf import settings
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone

from core.models import SiteConfig
from events.models import Category, Event

from .filters import WHEN_CHOICES, EventFilter, active_venues


def _browse_context(request):
    filters = EventFilter.from_request(request)
    now = timezone.now()

    main_feed = list(filters.main_feed(now)[:120])
    programs = list(filters.programs(now)[:120])

    return {
        "filters": filters,
        "events": main_feed,
        "programs": programs,
        "program_count": len(programs),
        "categories": Category.objects.filter(is_active=True),
        "venues": active_venues(),
        "when_choices": WHEN_CHOICES,
        "result_count": len(main_feed),
        "site_config": SiteConfig.load(),
    }


def index(request):
    """What's on.

    HTMX filter changes re-render only the results partial, so the map and the
    filter bar keep their state and the URL stays shareable.
    """
    context = _browse_context(request)

    if request.headers.get("HX-Request"):
        return render(request, "web/index.html#results", context)

    context["map_config"] = {
        "center": [settings.MAP_CENTER_LAT, settings.MAP_CENTER_LNG],
        "zoom": settings.MAP_ZOOM,
        "tileUrl": settings.TILE_URL,
        "attribution": settings.TILE_ATTRIBUTION,
        "geojsonUrl": "/events.geojson",
    }
    return render(request, "web/index.html", context)


def event_detail(request, slug):
    event = get_object_or_404(
        Event.objects.select_related("venue", "organizer").prefetch_related(
            "categories"
        ),
        slug=slug,
        status=Event.Status.PUBLISHED,
    )
    occurrences = list(event.upcoming_occurrences()[:25])

    return render(
        request,
        "web/event_detail.html",
        {
            "event": event,
            "occurrences": occurrences,
            "next_occurrence": occurrences[0] if occurrences else None,
            "schema_json": json.dumps(
                _schema_org(event, occurrences), ensure_ascii=False
            ),
            "site_config": SiteConfig.load(),
        },
    )


def _schema_org(event, occurrences):
    """schema.org Event markup.

    Worth the effort for a listing site: this is what puts events into search
    engines' event results, which reaches people who will never visit the
    site directly.
    """
    occurrence = occurrences[0] if occurrences else None

    data = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": event.title,
        "description": event.summary or event.description[:300],
        "eventStatus": "https://schema.org/EventScheduled",
        # Always offline: this site does not list online events at all.
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
    }

    if occurrence:
        data["startDate"] = occurrence.start.isoformat()
        if occurrence.end:
            data["endDate"] = occurrence.end.isoformat()

    if event.venue:
        location = {"@type": "Place", "name": event.venue.name}
        if event.venue.full_address:
            location["address"] = event.venue.full_address
        if event.venue.has_coordinates:
            location["geo"] = {
                "@type": "GeoCoordinates",
                "latitude": event.venue.latitude,
                "longitude": event.venue.longitude,
            }
        data["location"] = location

    if event.organizer:
        data["organizer"] = {
            "@type": "Organization",
            "name": event.organizer.name,
        }
        if event.organizer.website:
            data["organizer"]["url"] = event.organizer.website

    if event.is_free:
        data["offers"] = {
            "@type": "Offer",
            "price": "0",
            "priceCurrency": "CAD",
            "availability": "https://schema.org/InStock",
        }
    elif event.price_min is not None:
        data["offers"] = {
            "@type": "Offer",
            "price": str(event.price_min),
            "priceCurrency": "CAD",
        }
    if event.ticket_url and "offers" in data:
        data["offers"]["url"] = event.ticket_url

    if event.image_url:
        data["image"] = event.image_url

    return data


def events_geojson(request):
    """Map data for the current filter state.

    Reads the same query parameters as the browse view, so the pins always
    match the list rather than being a separate, drifting query.
    """
    filters = EventFilter.from_request(request)
    now = timezone.now()

    # Group events by venue so a place with three things on shows one pin.
    by_venue = {}
    for event in filters.mappable(now)[:400]:
        by_venue.setdefault(event.venue_id, {"venue": event.venue, "events": []})
        by_venue[event.venue_id]["events"].append(event)

    features = []
    for entry in by_venue.values():
        venue = entry["venue"]
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [venue.longitude, venue.latitude],
                },
                "properties": {
                    "venue": venue.name,
                    "address": venue.full_address,
                    "count": len(entry["events"]),
                    "events": [
                        {
                            "title": e.title,
                            "url": f"/events/{e.slug}/",
                            "start": e.next_start.isoformat() if e.next_start else None,
                            # Without the end, a consumer of this feed repeats
                            # the mistake the cards used to make and shows a
                            # three-day event as a single moment.
                            "end": e.next_end.isoformat() if e.next_end else None,
                            "free": e.is_free,
                            "series": e.is_series,
                        }
                        for e in entry["events"][:8]
                    ],
                },
            }
        )

    return JsonResponse({"type": "FeatureCollection", "features": features})


def healthz(request):
    """Liveness probe. Deliberately does not touch the database — a slow query
    should not take the pod out of rotation."""
    return HttpResponse("ok", content_type="text/plain")
