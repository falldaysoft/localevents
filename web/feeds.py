"""The RSS feed: listings as they are published, newest first.

Ordered by `published_at`, never `updated_at` — a moderator fixing a typo
would otherwise push a month-old listing back to the top of every reader.

Narrowed by the same query parameters as the browse page, through
`EventFilter`, so `?category=music` and `?venue=<slug>` are feeds of their own
without a second query to drift. That also means a listing whose dates have
all gone by drops out: announcing something that is already over helps
nobody, and a reader keeps the items it has already fetched.

Links are absolute against `settings.SITE_BASE_URL`, not the Sites table, for
the reason given in `web/sitemaps.py`.
"""

from django.conf import settings
from django.contrib.syndication.views import Feed
from django.urls import reverse
from django.utils import timezone
from django.utils.html import format_html_join

from events.models import Category, Event, Venue
from events.templatetags.event_dates import occurrence_when

from .filters import EventFilter

FEED_LENGTH = 30


def _absolute(path):
    return f"{settings.SITE_BASE_URL.rstrip('/')}{path}"


class LatestEventsFeed(Feed):
    def get_object(self, request):
        filters = EventFilter.from_request(request)
        query = request.GET.urlencode()
        path = reverse("events_rss") + (f"?{query}" if query else "")
        return {"filters": filters, "feed_path": path}

    def title(self, obj):
        filters = obj["filters"]
        if filters.venue:
            # The same rule as the venue page: a venue nobody has approved an
            # event at was typed by a submitter, and its name is not ours to
            # print.
            venue = (
                Venue.objects.filter(
                    slug=filters.venue, events__status=Event.Status.PUBLISHED
                )
                .distinct()
                .first()
            )
            if venue:
                return f"Events at {venue.name} — {settings.SITE_NAME}"
        if len(filters.categories) == 1:
            category = Category.objects.get(slug=filters.categories[0])
            return f"{category.name} events — {settings.SITE_NAME}"
        return f"New events — {settings.SITE_NAME}"

    def description(self, obj):
        return f"Events as they are added to {settings.SITE_NAME}."

    def link(self, obj):
        return _absolute(reverse("index"))

    def feed_url(self, obj):
        return _absolute(obj["feed_path"])

    def items(self, obj):
        return (
            obj["filters"]
            .apply(Event.objects.published(), timezone.now())
            .order_by("-published_at", "-pk")[:FEED_LENGTH]
        )

    def item_title(self, event):
        return event.title

    def item_link(self, event):
        return _absolute(reverse("event_detail", args=[event.slug]))

    def item_pubdate(self, event):
        return event.published_at

    def item_categories(self, event):
        return [c.name for c in event.categories.all()]

    def item_description(self, event):
        facts = [occurrence_when(event.next_start, event.next_end)]
        if event.venue:
            facts.append(event.venue.name)
        if event.price_display:
            facts.append(event.price_display)

        text = event.summary or event.description
        paragraphs = [" · ".join(facts)]
        if text:
            paragraphs.append(text)
        return format_html_join("", "<p>{}</p>", ((p,) for p in paragraphs))
