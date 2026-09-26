"""The sitemap: what a search engine should know exists.

Every URL here is absolute against `settings.SITE_BASE_URL`, not the Sites
table. `django.contrib.sitemaps` defaults to `Site.objects.get_current()`,
and a Sites row nobody ever edited says example.com — a sitemap full of
someone else's domain, which a search console reports only as "URL not
allowed" long after the fact.
"""

from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.sitemaps import Sitemap
from django.urls import reverse
from django.utils import timezone

from content.models import Page
from events.models import Category, Event, occurrence_overlaps

from .filters import active_venues

# An event page stays listed for a while after its last date: people search
# for something they went to, and the page still answers "when was it".
RECENTLY_PAST = timedelta(days=90)


class BaseSitemap(Sitemap):
    def get_urls(self, page=1, site=None, protocol=None):
        base = urlsplit(settings.SITE_BASE_URL)
        return self._urls(page, base.scheme or "https", base.netloc)


class StaticSitemap(BaseSitemap):
    changefreq = "daily"

    def items(self):
        return ["index", "venue_list", "category_list"]

    def location(self, item):
        return reverse(item)


class EventSitemap(BaseSitemap):
    """Published events with a date on now, to come, or not long gone.

    Not every event ever: a listing from three years ago is still a valid
    page, but offering the crawler an ever-growing pile of them spends its
    attention on things nobody can attend.
    """

    def items(self):
        return (
            Event.objects.published()
            .filter(occurrence_overlaps(timezone.now() - RECENTLY_PAST, prefix="occurrences"))
            .distinct()
            .order_by("pk")
        )

    def location(self, event):
        return reverse("event_detail", args=[event.slug])

    def lastmod(self, event):
        return event.updated_at


class VenueSitemap(BaseSitemap):
    def items(self):
        return active_venues()

    def location(self, venue):
        return reverse("venue_detail", args=[venue.slug])


class CategorySitemap(BaseSitemap):
    def items(self):
        return Category.objects.filter(is_active=True)

    def location(self, category):
        return reverse("category_detail", args=[category.slug])


class PageSitemap(BaseSitemap):
    def items(self):
        return Page.objects.filter(is_published=True).order_by("pk")

    def lastmod(self, page):
        return page.updated_at


SITEMAPS = {
    "static": StaticSitemap,
    "events": EventSitemap,
    "venues": VenueSitemap,
    "categories": CategorySitemap,
    "pages": PageSitemap,
}
