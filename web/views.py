import json
from datetime import date

from django.conf import settings
from django.http import HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from core.models import SiteConfig
from core.themes import template_name, theme_for
from events import interest
from events.models import Category, Event, Venue
from events.services import toggle_interest

from .filters import WHEN_CHOICES, EventFilter, active_venues
from .listing import _listing


def _page_start(request):
    """The day a reader asked the listing to start from, or None for now.

    Malformed or not in the future, it is ignored rather than raising — the
    same degrade-don't-500 rule the filters follow.
    """
    try:
        day = date.fromisoformat(request.GET.get("from", ""))
    except ValueError:
        return None
    return day if day > timezone.localdate() else None


def _viewer(request):
    """What the cards need to know about who is looking: their marks."""
    return {"interested_ids": interest.interested_event_ids(request)}


def _browse_context(request):
    filters = EventFilter.from_request(request)
    site_config = SiteConfig.load()
    # The horizon is the front page's rule. Any narrowing — a date range, a
    # category, a search — is a reader asking a question, and the answer
    # should not leave out a listing because it is quiet.
    horizon = None if filters.is_active else site_config.quiet_horizon_days
    return {
        **_listing(
            filters,
            timezone.now(),
            quiet_horizon_days=horizon,
            start=_page_start(request),
        ),
        **_viewer(request),
        # A "Later" link keeps the reader's filters; changing a filter drops
        # the page, since `from` is not a field in the form.
        "page_query": filters.querystring(),
        "filters": filters,
        "categories": Category.objects.filter(is_active=True),
        "venues": active_venues(),
        "when_choices": WHEN_CHOICES,
        "site_config": site_config,
    }


def index(request):
    """What's on.

    HTMX filter changes re-render only the results partial, so the map and the
    filter bar keep their state and the URL stays shareable.
    """
    context = _browse_context(request)
    template = template_name(theme_for(request), "web/index.html")

    if request.headers.get("HX-Request"):
        return render(request, f"{template}#results", context)

    context["map_config"] = {
        "center": [settings.MAP_CENTER_LAT, settings.MAP_CENTER_LNG],
        "zoom": settings.MAP_ZOOM,
        "tileUrl": settings.TILE_URL,
        "attribution": settings.TILE_ATTRIBUTION,
        "geojsonUrl": "/events.geojson",
    }
    return render(request, template, context)


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
        template_name(theme_for(request), "web/event_detail.html"),
        {
            "event": event,
            "occurrences": occurrences,
            "next_occurrence": occurrences[0] if occurrences else None,
            "schema_json": json.dumps(
                _schema_org(event, occurrences), ensure_ascii=False
            ),
            "site_config": SiteConfig.load(),
            "can_promote": _sponsorships_on_sale(),
            **_viewer(request),
        },
    )


def _sponsorships_on_sale():
    from sponsors.models import SponsorConfig

    return SponsorConfig.load().on_sale


@require_POST
def event_interest(request, slug):
    """The reader's star: toggle an "Interested" mark on a published event.

    POST only, behind CSRF — that pair is the bot protection, and it is enough
    for the traffic this is worried about (see `events.interest`). An
    anonymous reader gets their visitor cookie here, on the first click,
    rather than on page view.

    Under HTMX the button swaps itself; otherwise the reader is sent back to
    the page they pressed it on, if it is one of ours.
    """
    event = get_object_or_404(Event, slug=slug, status=Event.Status.PUBLISHED)
    if not interest.within_rate_limit(request):
        return HttpResponse("Too many marks from this address. Try later.", status=429)

    new_visitor = None
    if request.user.is_authenticated:
        now_interested = toggle_interest(event, user=request.user)
    else:
        vid = interest.visitor_id(request)
        if vid is None:
            vid = new_visitor = interest.new_visitor_id()
        now_interested = toggle_interest(
            event, fingerprint=interest.fingerprint_for(vid)
        )

    event.refresh_from_db(fields=["interest_count"])
    back = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(
        back, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        back = reverse("event_detail", args=[event.slug])

    if request.headers.get("HX-Request"):
        # The swapped-in form keeps the page it was pressed on as its `next`,
        # or a later no-script fallback would land on this URL and 405.
        response = render(
            request,
            template_name(theme_for(request), "web/_interest_button.html"),
            {"event": event, "interested": now_interested, "next_url": back},
        )
    else:
        response = HttpResponseRedirect(back)

    if new_visitor is not None:
        interest.set_visitor_cookie(response, new_visitor)
    return response


def my_events(request):
    """What this viewer has marked, and what they have submitted.

    Open to anonymous readers too: the site remembers their marks on this
    device, and the page is where that promise is kept. Marked events with a
    date still to come go through `_listing`, so they collapse and tier
    exactly as the front page does; the ones that have passed are listed by
    name, because a marked event is still worth finding after the fact.
    Submissions are an account's, so that half appears only when signed in.
    """
    now = timezone.now()
    marked = interest.interested_event_ids(request)
    context = _listing(EventFilter(ids=marked), now, paged=False)
    shown = {e.pk for e in context["events"] + context["quiet"] + context["programs"]}
    past = (
        Event.objects.published()
        .filter(pk__in=marked - shown)
        .order_by("-created_at")
    )

    submissions = None
    if request.user.is_authenticated:
        from submissions.models import Submission

        submissions = Submission.objects.filter(submitted_by=request.user)

    context.update(
        {
            "interested_ids": marked,
            "past": list(past),
            "submissions": submissions,
            "site_config": SiteConfig.load(),
        }
    )
    return render(
        request, template_name(theme_for(request), "web/my_events.html"), context
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


def _map_config(venue):
    return {
        "lat": venue.latitude,
        "lng": venue.longitude,
        "tileUrl": settings.TILE_URL,
        "attribution": settings.TILE_ATTRIBUTION,
    }


def venue_list(request):
    """Every venue with something on, for crawlers as much as for people.

    Only venues with an upcoming published event: a list of halls with nothing
    in them is not worth a click.
    """
    return render(
        request,
        template_name(theme_for(request), "web/landing_index.html"),
        {
            "kind": "venues",
            "heading": "Venues",
            "lede": "Places with something coming up.",
            "items": [
                {
                    "url": reverse("venue_detail", args=[venue.slug]),
                    "name": venue.name,
                    "detail": venue.full_address,
                }
                for venue in active_venues()
            ],
            "site_config": SiteConfig.load(),
        },
    )


def venue_detail(request, slug):
    """Everything on at one place.

    A venue is created the moment someone submits an event, before anyone has
    looked at it, so a venue that has never hosted a published event 404s —
    otherwise a rejected submission would publish its address anyway. One
    with nothing *upcoming* still renders, because links to it outlive a
    season, but asks not to be indexed until it has something on.
    """
    venue = get_object_or_404(
        Venue.objects.filter(events__status=Event.Status.PUBLISHED).distinct(),
        slug=slug,
    )
    context = _listing(
        EventFilter(venue=venue.slug), timezone.now(), start=_page_start(request)
    )
    context.update(
        {
            "venue": venue,
            "heading": venue.name,
            "page_title": f"Events at {venue.name}",
            "lede": venue.full_address,
            "crumb_url": reverse("venue_list"),
            "crumb_label": "Venues",
            "canonical": reverse("venue_detail", args=[venue.slug]),
            "feed_url": f"{reverse('events_rss')}?venue={venue.slug}",
            "map_config": _map_config(venue) if venue.has_coordinates else None,
            "schema_json": json.dumps(_venue_schema(venue), ensure_ascii=False),
            "empty_title": "Nothing scheduled here right now.",
            "site_config": SiteConfig.load(),
            **_viewer(request),
        }
    )
    return render(
        request, template_name(theme_for(request), "web/landing.html"), context
    )


def _venue_schema(venue):
    data = {"@context": "https://schema.org", "@type": "Place", "name": venue.name}
    if venue.full_address:
        data["address"] = venue.full_address
    if venue.has_coordinates:
        data["geo"] = {
            "@type": "GeoCoordinates",
            "latitude": venue.latitude,
            "longitude": venue.longitude,
        }
    if venue.website:
        data["url"] = venue.website
    return data


def category_list(request):
    now = timezone.now()
    items = []
    for category in Category.objects.filter(is_active=True):
        count = EventFilter(categories=[category.slug]).main_feed(now).count()
        items.append(
            {
                "url": reverse("category_detail", args=[category.slug]),
                "name": f"{category.emoji} {category.name}".strip(),
                "detail": category.description,
                "count": count,
            }
        )
    return render(
        request,
        template_name(theme_for(request), "web/landing_index.html"),
        {
            "kind": "categories",
            "heading": "Categories",
            "lede": "Browse by what kind of thing it is.",
            "items": items,
            "site_config": SiteConfig.load(),
        },
    )


def category_detail(request, slug):
    category = get_object_or_404(Category, slug=slug, is_active=True)
    context = _listing(
        EventFilter(categories=[category.slug]),
        timezone.now(),
        start=_page_start(request),
    )
    context.update(
        {
            "category": category,
            "heading": f"{category.emoji} {category.name}".strip(),
            "page_title": f"{category.name} events",
            "lede": category.description,
            "crumb_url": reverse("category_list"),
            "crumb_label": "Categories",
            "canonical": reverse("category_detail", args=[category.slug]),
            "feed_url": f"{reverse('events_rss')}?category={category.slug}",
            "empty_title": "Nothing in this category coming up.",
            "site_config": SiteConfig.load(),
            **_viewer(request),
        }
    )
    return render(
        request, template_name(theme_for(request), "web/landing.html"), context
    )


def robots_txt(request):
    """Keep crawlers on the listings.

    The filtered front page (`/?when=…&category=…`) is disallowed because its
    parameter combinations are an unbounded crawl of near-duplicates of `/`;
    the venue and category pages are the indexable form of the same views.
    Like /healthz, this touches no database.
    """
    lines = [
        "User-agent: *",
        "Disallow: /admin/",
        "Disallow: /moderate/",
        "Disallow: /accounts/",
        "Disallow: /claim/",
        "Disallow: /profile/",
        "Disallow: /submit/",
        "Disallow: /api/",
        "Disallow: /events.geojson",
        "Disallow: /?",
        "",
        f"Sitemap: {settings.SITE_BASE_URL.rstrip('/')}{reverse('sitemap')}",
    ]
    return HttpResponse("\n".join(lines) + "\n", content_type="text/plain")


def healthz(request):
    """Liveness probe. Deliberately does not touch the database — a slow query
    should not take the pod out of rotation."""
    return HttpResponse("ok", content_type="text/plain")
