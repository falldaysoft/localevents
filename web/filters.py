"""Turning a query string into a set of events.

The filter state lives entirely in the URL. That is deliberate: a filtered view
is the thing people send each other ("here's what's free this weekend"), and it
is also what the ICS and map endpoints consume, so all three read the same
parameters and cannot drift apart.
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from django.db.models import OuterRef, Q, Subquery
from django.utils import timezone

from events.models import Category, Event, Occurrence, Venue, occurrence_overlaps

WHEN_CHOICES = [
    ("", "Any time"),
    ("today", "Today"),
    ("weekend", "This weekend"),
    ("week", "Next 7 days"),
    ("month", "Next 30 days"),
]


def _day_bounds(day, tz):
    """Start and end of a local calendar day, as aware datetimes."""
    start = timezone.make_aware(datetime.combine(day, time.min), tz)
    end = timezone.make_aware(datetime.combine(day, time.max), tz)
    return start, end


@dataclass
class EventFilter:
    """Parsed, validated browse parameters.

    Unknown or malformed values are ignored rather than raising — a filter link
    someone edited by hand should degrade to a broader view, not a 500.
    """

    when: str = ""
    categories: list = field(default_factory=list)
    free_only: bool = False
    family_only: bool = False
    hide_commercial: bool = False
    venue: str = ""
    query: str = ""
    include_programs: bool = False

    @classmethod
    def from_request(cls, request):
        params = request.GET
        when = params.get("when", "").strip()
        if when not in {c[0] for c in WHEN_CHOICES}:
            when = ""

        valid_slugs = set(
            Category.objects.filter(is_active=True).values_list("slug", flat=True)
        )
        categories = [c for c in params.getlist("category") if c in valid_slugs]

        return cls(
            when=when,
            categories=categories,
            free_only=params.get("free") == "1",
            family_only=params.get("family") == "1",
            hide_commercial=params.get("noncommercial") == "1",
            venue=params.get("venue", "").strip(),
            query=params.get("q", "").strip()[:200],
            include_programs=params.get("programs") == "1",
        )

    @property
    def is_active(self):
        """Is anything actually narrowed? Drives the 'clear filters' affordance."""
        return any(
            [
                self.when,
                self.categories,
                self.free_only,
                self.family_only,
                self.hide_commercial,
                self.venue,
                self.query,
            ]
        )

    def date_range(self, now=None):
        """(start, end) datetimes for the chosen window, or (now, None)."""
        now = now or timezone.now()
        tz = timezone.get_current_timezone()
        today = timezone.localtime(now, tz).date()

        if self.when == "today":
            return _day_bounds(today, tz)

        if self.when == "weekend":
            # Saturday and Sunday of the coming weekend. On a Saturday or
            # Sunday that means this one, not next — "this weekend" should not
            # mean "in six days" when asked on a Saturday morning.
            days_until_saturday = (5 - today.weekday()) % 7
            saturday = today + timedelta(days=days_until_saturday)
            sunday = saturday + timedelta(days=1)
            start, _ = _day_bounds(saturday, tz)
            _, end = _day_bounds(sunday, tz)
            return max(start, now), end

        if self.when == "week":
            return now, now + timedelta(days=7)

        if self.when == "month":
            return now, now + timedelta(days=30)

        return now, None

    def occurrence_window(self, now=None, prefix="occurrences"):
        """Q object restricting occurrences to the chosen window.

        Overlap, not containment — see `events.models.occurrence_overlaps`. A
        three-day festival is on all three days, so it answers to Today on each
        of them rather than only on the day it began.
        """
        start, end = self.date_range(now)
        cancelled = f"{prefix}__is_cancelled" if prefix else "is_cancelled"
        return occurrence_overlaps(start, end, prefix=prefix) & Q(
            **{cancelled: False}
        )

    def apply(self, queryset, now=None):
        """Narrow `queryset` and annotate each event with its next date.

        The annotation is what collapses a series: an event appears once, at
        its next relevant occurrence, however many dates it actually has.
        """
        now = now or timezone.now()

        if self.categories:
            queryset = queryset.filter(categories__slug__in=self.categories)
        if self.free_only:
            queryset = queryset.filter(is_free=True)
        if self.family_only:
            queryset = queryset.filter(is_family_friendly=True)
        if self.hide_commercial:
            queryset = queryset.filter(is_commercial=False)
        if self.venue:
            queryset = queryset.filter(venue__slug=self.venue)
        if self.query:
            queryset = queryset.filter(
                Q(title__icontains=self.query)
                | Q(summary__icontains=self.query)
                | Q(description__icontains=self.query)
                | Q(venue__name__icontains=self.query)
                | Q(organizer__name__icontains=self.query)
            )

        # The start and end have to come from the *same* occurrence, so this is
        # a subquery rather than two aggregates. Min(start) and Min(end) over
        # the same set can straddle two different dates — and do, the moment one
        # occurrence has an end and another does not, since Min skips nulls.
        soonest = Occurrence.objects.filter(
            self.occurrence_window(now, prefix=""), event=OuterRef("pk")
        ).order_by("start")
        queryset = queryset.annotate(
            next_start=Subquery(soonest.values("start")[:1]),
            next_end=Subquery(soonest.values("end")[:1]),
        ).filter(next_start__isnull=False)

        return (
            queryset.select_related("venue", "organizer")
            .prefetch_related("categories")
            .distinct()
        )

    def main_feed(self, now=None):
        """Featured and Listed, most prominent first then soonest."""
        return self.apply(Event.objects.in_main_feed(), now).order_by(
            "-prominence", "next_start"
        )

    def programs(self, now=None):
        """Background listings — the regular weekly things.

        Ordered by name rather than date: a standing program is something you
        look up, not something you find out about.
        """
        return self.apply(Event.objects.programs(), now).order_by("title")

    def mappable(self, now=None):
        """Everything with coordinates, for the map.

        Includes programs regardless of the toggle — hiding a venue's pin
        because a class is 'background' would make the map look wrong.
        """
        queryset = self.apply(Event.objects.published(), now)
        return queryset.filter(
            venue__isnull=False,
            venue__latitude__isnull=False,
            venue__longitude__isnull=False,
        ).order_by("next_start")

    def querystring(self, **overrides):
        """Rebuild the query string, optionally changing one parameter.

        Used by the filter chips so toggling one thing preserves the rest.
        """
        from urllib.parse import urlencode

        params = []
        values = {
            "when": self.when,
            "free": "1" if self.free_only else "",
            "family": "1" if self.family_only else "",
            "noncommercial": "1" if self.hide_commercial else "",
            "venue": self.venue,
            "q": self.query,
            "programs": "1" if self.include_programs else "",
        }
        values.update({k: v for k, v in overrides.items() if k != "category"})

        for key, value in values.items():
            if value:
                params.append((key, value))

        categories = overrides.get("category", self.categories)
        for slug in categories:
            params.append(("category", slug))

        return urlencode(params)


def active_venues():
    """Venues with at least one published event on now or still to come."""
    now = timezone.now()
    return (
        Venue.objects.filter(
            occurrence_overlaps(now, prefix="events__occurrences"),
            events__status=Event.Status.PUBLISHED,
            events__occurrences__is_cancelled=False,
        )
        .distinct()
        .order_by("name")
    )
