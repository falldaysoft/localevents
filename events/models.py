"""The event domain.

There is deliberately no concept of an online event. A listing for something
happening anywhere has no local connection to verify, which makes it the
easiest possible thing to spam a community site with — so the model does not
represent it at all, rather than offering it and filtering it later.

Two ideas carry most of the weight here, and they are deliberately independent:

`Event.listing_type` decides how an event is *collapsed*. A weekly class is one
event with many occurrences; it must never emit fifty cards into a feed.

`Event.prominence` decides *placement*, and only a moderator sets it. A farmers
market and a paid pottery class can both be weekly series while belonging in
very different parts of the site. Conflating collapsing with placement is how a
community listing turns into noise.

Everything a visitor browses queries `Occurrence`, not `Event` — "what's on
this weekend" is a question about dates.
"""

import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone
from django.utils.text import slugify


def _unique_slug(instance, value, field_name="slug"):
    """Slugify `value`, appending a short suffix if it is already taken."""
    base = slugify(value)[:200] or "item"
    model = instance.__class__
    slug = base
    while (
        model.objects.filter(**{field_name: slug})
        .exclude(pk=instance.pk)
        .exists()
    ):
        slug = f"{base}-{secrets.token_hex(3)}"
    return slug


# ---------------------------------------------------------------------------
# Geocoding
# ---------------------------------------------------------------------------


class GeocodeThrottle(models.Model):
    """A cluster-wide lease enforcing Nominatim's 1 request/second limit.

    The limit applies to the whole application, not to one process, so an
    in-process sleep is not enough: `db_worker` can run several workers and,
    later, several pods. Callers take a row lock, wait out whatever remains of
    the second, stamp it, and release.

    Volume is low in the steady state because a venue geocodes once and caches
    forever. The case this exists for is a bulk import introducing many new
    venues at once, which is exactly when getting it wrong would matter.
    """

    MIN_INTERVAL = timedelta(seconds=1)

    last_called_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "geocode throttle"
        verbose_name_plural = "geocode throttle"

    def __str__(self):
        return f"Geocode throttle (last called {self.last_called_at or 'never'})"

    @classmethod
    def seconds_to_wait(cls, now=None):
        """How long a caller must wait, without taking the lock.

        Split out so it can be tested without a live transaction.
        """
        now = now or timezone.now()
        row = cls.objects.filter(pk=1).first()
        if row is None or row.last_called_at is None:
            return 0.0
        elapsed = now - row.last_called_at
        remaining = cls.MIN_INTERVAL - elapsed
        return max(0.0, remaining.total_seconds())

    @classmethod
    @transaction.atomic
    def acquire(cls, sleep=None):
        """Block until it is safe to call Nominatim, then stamp the row.

        Holds a row-level lock for the duration, so concurrent workers queue up
        behind each other rather than all deciding independently that enough
        time has passed.
        """
        import time

        sleep = sleep or time.sleep

        row = cls.objects.select_for_update().filter(pk=1).first()
        if row is None:
            row = cls.objects.create(pk=1)
            row = cls.objects.select_for_update().get(pk=1)

        if row.last_called_at is not None:
            remaining = (
                cls.MIN_INTERVAL - (timezone.now() - row.last_called_at)
            ).total_seconds()
            if remaining > 0:
                sleep(remaining)

        row.last_called_at = timezone.now()
        row.save(update_fields=["last_called_at"])
        return row


# ---------------------------------------------------------------------------
# Places and people
# ---------------------------------------------------------------------------


class Venue(models.Model):
    """A physical place, geocoded once and reused.

    Events recur at the same handful of places, so caching coordinates here
    rather than per-event keeps the map nearly free and makes "everything at
    the community centre" a query rather than a text search.
    """

    class GeocodeStatus(models.TextChoices):
        PENDING = "pending", "Not yet geocoded"
        OK = "ok", "Geocoded"
        FAILED = "failed", "Geocoding failed"
        OUT_OF_REGION = "out_of_region", "Outside the region"
        MANUAL = "manual", "Set by hand"

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    address = models.CharField(max_length=300, blank=True)
    city = models.CharField(max_length=120, blank=True)
    postal_code = models.CharField(max_length=20, blank=True)
    website = models.URLField(blank=True)
    notes = models.TextField(blank=True)

    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)
    geocode_status = models.CharField(
        max_length=20, choices=GeocodeStatus.choices, default=GeocodeStatus.PENDING
    )
    geocode_attempted_at = models.DateTimeField(null=True, blank=True)
    geocode_error = models.CharField(max_length=300, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [models.Index(fields=["geocode_status"])]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(self, self.name)
        super().save(*args, **kwargs)

    @property
    def has_coordinates(self):
        return self.latitude is not None and self.longitude is not None

    @property
    def full_address(self):
        parts = [self.address, self.city, self.postal_code]
        return ", ".join(p for p in parts if p)

    def geocode_query(self):
        """The string handed to the geocoder.

        Includes the venue name because community venues are often better known
        by name than by street address.
        """
        parts = [self.name, self.address, self.city, self.postal_code]
        return ", ".join(p for p in parts if p)

    def geocode_queries(self):
        """Progressively less specific queries, best first.

        Nominatim's free-text search is an all-or-nothing match, so a name and
        a street address together can return *nothing* when either alone would
        resolve — "Paris Fairgrounds, 139 Silver Street, Paris, ON" found zero
        results while both halves found one each. A community venue is exactly
        the case where that happens: OSM knows it as a named feature, the
        submitter typed the mailing address, and the two do not line up.

        The address-only rung comes before the name-only one because a street
        address is a precise claim, while a name match is whatever OSM decided
        to call something nearby.
        """
        name = (self.name or "").strip()
        address = (self.address or "").strip()
        location = [p.strip() for p in (self.city, self.postal_code) if p and p.strip()]

        # Each rung is (what identifies the place, the rest). A rung with no
        # identifying part is just a town name, which would geocode to the
        # middle of the town and put a marker somewhere no event is.
        candidates = [
            (name, [name, address, *location]),
            (address, [address, *location]),
            (name, [name, *location]),
        ]

        queries = []
        for identifier, parts in candidates:
            if not identifier:
                continue
            query = ", ".join(p for p in parts if p)
            # With no street address, rungs one and three are the same string;
            # asking twice would just spend another second of the rate limit.
            if query not in queries:
                queries.append(query)
        return queries

    @staticmethod
    def coordinates_in_region(latitude, longitude):
        """Is this point inside the instance's configured bounds?

        MAP_BBOX doubles as a region gate: an event that geocodes outside it is
        flagged for a moderator rather than published, which is what keeps a
        local site local without anyone policing it by hand.
        """
        min_lat, min_lng, max_lat, max_lng = settings.MAP_BBOX
        return min_lat <= latitude <= max_lat and min_lng <= longitude <= max_lng

    @property
    def is_in_region(self):
        if not self.has_coordinates:
            return None
        return self.coordinates_in_region(self.latitude, self.longitude)


class Organizer(models.Model):
    """Whoever is putting the event on.

    `is_commercial` is not a value judgement — a paid workshop is legitimate
    community information. It exists so visitors can filter, and so the default
    feed can weight community events without excluding anything.
    """

    name = models.CharField(max_length=200)
    slug = models.SlugField(max_length=220, unique=True, blank=True)
    website = models.URLField(blank=True)
    contact_email = models.EmailField(blank=True)
    description = models.TextField(blank=True)
    is_commercial = models.BooleanField(
        default=False,
        help_text="Sells a product or service. Shown as a label and filterable; "
        "not grounds for rejection on its own.",
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="organizers",
        help_text="The account that may edit this organizer's listings.",
    )
    max_active_series = models.PositiveSmallIntegerField(
        default=5,
        help_text="How many recurring listings this organizer may run at once. "
        "Stops any single submitter from dominating the Programs section.",
    )

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = _unique_slug(self, self.name)
        super().save(*args, **kwargs)

    def active_series_count(self):
        return self.events.filter(
            listing_type=Event.ListingType.SERIES,
            status=Event.Status.PUBLISHED,
        ).count()

    def has_series_capacity(self):
        return self.active_series_count() < self.max_active_series


class Category(models.Model):
    """A curated browse facet.

    Kept deliberately small and moderator-controlled. Free-form tags would
    make the filter bar useless within a month; the value of a category is that
    it reliably means the same thing.
    """

    name = models.CharField(max_length=60, unique=True)
    slug = models.SlugField(max_length=70, unique=True)
    emoji = models.CharField(max_length=8, blank=True)
    description = models.CharField(max_length=200, blank=True)
    sort_order = models.PositiveSmallIntegerField(default=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["sort_order", "name"]
        verbose_name_plural = "categories"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def occurrence_overlaps(window_start, window_end=None, prefix=""):
    """A Q matching occurrences that are on at any point in a window.

    The obvious test — "does it start inside the window" — quietly loses every
    event that spans days. A festival running Friday evening to Sunday
    afternoon started before Saturday, so on Saturday it vanishes from Today,
    from This Weekend, from the map and from the feed, while it is *actually
    happening*. That is the worst possible time for a listing site to hide it.

    So the test is overlap, not containment: the occurrence has begun by the
    end of the window, and has not finished before the window starts. An
    occurrence with no stated end is treated as ending when it starts, which
    makes this identical to the old behaviour for the single-instant case.

    `prefix` lets a caller ask about a related name, e.g. "occurrences".
    """
    field = f"{prefix}__" if prefix else ""

    not_over = models.Q(**{f"{field}end__gte": window_start}) | models.Q(
        **{f"{field}end__isnull": True, f"{field}start__gte": window_start}
    )
    if window_end is None:
        return not_over
    return models.Q(**{f"{field}start__lte": window_end}) & not_over


class OccurrenceQuerySet(models.QuerySet):
    def live(self, now=None):
        """Not cancelled, and not yet over."""
        now = now or timezone.now()
        return self.filter(
            occurrence_overlaps(now), is_cancelled=False
        )


class EventQuerySet(models.QuerySet):
    def published(self):
        return self.filter(status=Event.Status.PUBLISHED)

    def one_off(self):
        return self.filter(listing_type=Event.ListingType.ONE_OFF)

    def series(self):
        return self.filter(listing_type=Event.ListingType.SERIES)

    def in_main_feed(self):
        """Featured and Listed only.

        Background listings — the regular weekly programs — are reachable by
        filter and in their own section, but they do not compete with a one-off
        concert for the front page.
        """
        return self.published().exclude(prominence=Event.Prominence.BACKGROUND)

    def programs(self):
        return self.published().filter(prominence=Event.Prominence.BACKGROUND)

    def needing_renewal(self, within_days=14, now=None):
        """Series whose run is ending soon and that have not been reminded.

        A series expires rather than lingering forever; the submitter gets one
        click to keep it. That is much less friction than refiling, and it is
        what keeps the Programs section honest.
        """
        now = now or timezone.now()
        return self.published().series().filter(
            series_ends_on__isnull=False,
            series_ends_on__lte=(now + timedelta(days=within_days)).date(),
            series_ends_on__gte=now.date(),
            last_renewal_email_at__isnull=True,
        )

    def expired_series(self, now=None):
        now = now or timezone.now()
        return self.published().series().filter(
            series_ends_on__isnull=False, series_ends_on__lt=now.date()
        )


class Event(models.Model):
    """A thing happening, once or repeatedly.

    An Event carries the description; an Occurrence carries a date. Recurrence
    is modelled rather than duplicated because retrofitting it later means
    rewriting every query that touches a calendar.
    """

    class ListingType(models.TextChoices):
        ONE_OFF = "one_off", "One-off event"
        SERIES = "series", "Recurring series"

    class Prominence(models.IntegerChoices):
        """How much room this gets. Set by a moderator, never by a submitter.

        Ordered so that a plain descending sort puts the most prominent first.
        """

        BACKGROUND = 1, "Background — regular program"
        LISTED = 2, "Listed — appears in the main feed"
        FEATURED = 3, "Featured — highlighted"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        PENDING = "pending", "Awaiting review"
        PUBLISHED = "published", "Published"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"

    class Source(models.TextChoices):
        SUBMISSION = "submission", "Submitted"
        IMPORT = "import", "Imported from a feed"
        MANUAL = "manual", "Added by a moderator"

    title = models.CharField(max_length=250)
    slug = models.SlugField(max_length=270, unique=True, blank=True)
    summary = models.CharField(
        max_length=300, blank=True, help_text="One line, shown on cards."
    )
    description = models.TextField(blank=True)

    organizer = models.ForeignKey(
        Organizer, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="events",
    )
    venue = models.ForeignKey(
        Venue, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="events",
        help_text="Leave empty only if the location is still to be confirmed.",
    )

    source_url = models.URLField(
        blank=True, help_text="Where this listing came from."
    )
    ticket_url = models.URLField(blank=True)
    image_url = models.URLField(blank=True)

    is_free = models.BooleanField(default=False)
    price_min = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )
    price_max = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )
    price_note = models.CharField(max_length=120, blank=True)

    age_min = models.PositiveSmallIntegerField(null=True, blank=True)
    age_max = models.PositiveSmallIntegerField(null=True, blank=True)
    is_family_friendly = models.BooleanField(default=False)
    accessibility_notes = models.TextField(blank=True)

    categories = models.ManyToManyField(Category, blank=True, related_name="events")

    listing_type = models.CharField(
        max_length=10, choices=ListingType.choices, default=ListingType.ONE_OFF
    )
    prominence = models.IntegerField(
        choices=Prominence.choices,
        default=Prominence.LISTED,
        help_text="Where this sits in the feed. A moderator's call.",
    )
    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.DRAFT
    )
    source = models.CharField(
        max_length=12, choices=Source.choices, default=Source.SUBMISSION
    )
    is_commercial = models.BooleanField(default=False)

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="submitted_events",
    )

    # Series lifecycle. A series is listed for a bounded run and then either
    # renewed in one click or allowed to lapse.
    series_ends_on = models.DateField(null=True, blank=True)
    renewal_token = models.CharField(max_length=64, blank=True, db_index=True)
    last_renewal_email_at = models.DateTimeField(null=True, blank=True)

    # Denormalised so the Rising queue and tie-breaking can order without a
    # join. Kept in step by Interest.save()/delete().
    interest_count = models.PositiveIntegerField(default=0, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(null=True, blank=True)

    objects = EventQuerySet.as_manager()

    class Meta:
        ordering = ["-prominence", "-published_at"]
        indexes = [
            models.Index(fields=["status", "prominence"]),
            models.Index(fields=["listing_type", "status"]),
        ]

    def __str__(self):
        return self.title

    def assign_slug(self, start=None):
        """Settle the permanent slug, dating a one-off with the year it runs.

        Only a one-off is dated. Its year is a permanent fact about it, and
        this year's fair sitting beside last year's is the collision worth
        resolving — without the year the second one lands on a random hex
        suffix, which tells a reader nothing. A series has no single date:
        stamping a weekly market with its first occurrence would file it under
        the year it was *added*, and read as stale every year after.

        The caller passes the date rather than this reading `self.occurrences`
        because a slug is never rewritten — links get shared — so the year has
        to be known before the first save, and a brand-new event has no
        occurrences yet.
        """
        text = self.title
        if start and self.listing_type == self.ListingType.ONE_OFF:
            if timezone.is_aware(start):
                start = timezone.localtime(start)
            # Slugify before appending so a very long title is truncated
            # without taking the year down with it.
            text = f"{slugify(self.title)[:195] or 'event'}-{start.year}"
        self.slug = _unique_slug(self, text)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.assign_slug()
        if self.status == self.Status.PUBLISHED and self.published_at is None:
            self.published_at = timezone.now()
        if self.listing_type == self.ListingType.SERIES and not self.renewal_token:
            self.renewal_token = secrets.token_urlsafe(32)
        super().save(*args, **kwargs)

    @property
    def is_series(self):
        return self.listing_type == self.ListingType.SERIES

    @property
    def price_display(self):
        if self.is_free:
            return "Free"
        if self.price_note:
            return self.price_note
        if self.price_min is None and self.price_max is None:
            return ""
        if self.price_max in (None, self.price_min):
            return f"${self.price_min:,.2f}".rstrip("0").rstrip(".")
        return f"${self.price_min:,.0f}–${self.price_max:,.0f}"

    def next_occurrence(self, now=None):
        """The one that is on now, or failing that the next one to come.

        Ordering by start rather than by end means an in-progress span is
        returned ahead of a date later today, which is the right answer to
        "what is happening here" while it is happening.
        """
        return self.upcoming_occurrences(now).first()

    def upcoming_occurrences(self, now=None):
        return self.occurrences.live(now).order_by("start")

    def needs_region_review(self):
        """True when the venue sits outside the instance's bounds.

        Not an automatic rejection — plenty of communities care about something
        just over the county line — but a moderator should look.
        """
        if self.venue is None:
            return False
        return self.venue.is_in_region is False


class Occurrence(models.Model):
    """One dated instance of an event.

    Every browse, map, and calendar query goes through here. A one-off event
    has exactly one of these, which keeps those queries uniform.
    """

    event = models.ForeignKey(
        Event, on_delete=models.CASCADE, related_name="occurrences"
    )
    start = models.DateTimeField()
    end = models.DateTimeField(null=True, blank=True)
    is_cancelled = models.BooleanField(default=False)
    note = models.CharField(
        max_length=200, blank=True,
        help_text="Anything specific to this date, e.g. 'Guest speaker'.",
    )

    objects = OccurrenceQuerySet.as_manager()

    class Meta:
        ordering = ["start"]
        indexes = [
            models.Index(fields=["start"]),
            models.Index(fields=["event", "start"]),
            # The overlap test reads `end` for every candidate occurrence, on
            # every browse query. Without this it is a scan.
            models.Index(fields=["end"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["event", "start"], name="unique_event_start"
            ),
            models.CheckConstraint(
                condition=models.Q(end__isnull=True) | models.Q(end__gt=models.F("start")),
                name="occurrence_ends_after_it_starts",
            ),
        ]

    def __str__(self):
        return f"{self.event.title} — {self.start:%Y-%m-%d %H:%M}"

    @property
    def finishes_at(self):
        """When this is over. An occurrence with no stated end is an instant."""
        return self.end or self.start

    @property
    def has_ended(self):
        return self.finishes_at < timezone.now()

    @property
    def is_under_way(self):
        return self.start <= timezone.now() <= self.finishes_at

    @property
    def spans_days(self):
        """Does this run past midnight into another local day?

        Asked by every template that formats a date, because "Fri 5 Sep, 6:00
        pm – 5:00 pm" is not a thing that can be read.
        """
        if self.end is None:
            return False
        return timezone.localtime(self.start).date() != timezone.localtime(self.end).date()


class Interest(models.Model):
    """Someone marking that they might go.

    Deliberately "Interested", not an upvote — an upvote is a judgement and
    invites brigading, while interest is a genuine signal and later a useful
    hook for a reminder.

    Anonymous interest is allowed with only cookie/IP-derived deduplication,
    which is weak. That is acceptable *because the signal's power is bounded*:
    it surfaces events in the moderators' Rising queue and breaks ties within a
    prominence tier, but it can never move an event between tiers. The worst a
    determined faker achieves is putting their event in front of a human who
    then exercises judgement.
    """

    event = models.ForeignKey(
        Event, on_delete=models.CASCADE, related_name="interests"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name="interests",
    )
    fingerprint = models.CharField(
        max_length=64,
        blank=True,
        help_text="Hash of a per-visitor cookie id and IP, for anonymous marks.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["event", "user"],
                condition=models.Q(user__isnull=False),
                name="unique_interest_per_user",
            ),
            models.UniqueConstraint(
                fields=["event", "fingerprint"],
                condition=models.Q(user__isnull=True),
                name="unique_interest_per_fingerprint",
            ),
        ]

    def __str__(self):
        who = self.user.display_name if self.user else "anonymous"
        return f"{who} interested in {self.event.title}"

    def save(self, *args, **kwargs):
        created = self.pk is None
        super().save(*args, **kwargs)
        if created:
            Event.objects.filter(pk=self.event_id).update(
                interest_count=models.F("interest_count") + 1
            )

    def delete(self, *args, **kwargs):
        event_id = self.event_id
        super().delete(*args, **kwargs)
        Event.objects.filter(pk=event_id, interest_count__gt=0).update(
            interest_count=models.F("interest_count") - 1
        )


# ---------------------------------------------------------------------------
# Re-reading a source page
# ---------------------------------------------------------------------------


class EventRefreshQuerySet(models.QuerySet):
    def awaiting_review(self):
        return self.filter(status=EventRefresh.Status.READY)

    def stranded(self, now=None):
        """Refreshes whose worker is never coming back. See Submission.stranded."""
        from submissions.models import STALE_ENRICHMENT_AFTER

        now = now or timezone.now()
        return self.filter(
            status__in=[EventRefresh.Status.QUEUED, EventRefresh.Status.READING],
            updated_at__lt=now - STALE_ENRICHMENT_AFTER,
        )


class EventRefresh(models.Model):
    """A re-read of an event's source page, held until a human accepts it.

    Two things go stale after a listing is approved: the page itself — a time
    moves, a date is added — and *our* reading of it. An event entered before
    occurrences carried their own hours has one date where the page always
    listed six, and no amount of re-reading the old draft recovers them. Both
    are fixed by reading the page again.

    What is deliberately *not* here is applying the result. A refresh never
    writes to its event on its own, however confident the extraction looks.
    The same rule that governs submissions holds after publication and matters
    more, because there is no submitter left in the loop: a moderator sees each
    field the page now disagrees with and ticks the ones to take. An
    auto-applying refresh would let a rewritten page silently replace a listing
    a human had already checked, and the first anyone would know of it is a
    reader turning up on the wrong evening.

    Storing the proposal rather than diffing on the fly is not bookkeeping for
    its own sake: extraction measured 116s and 336s against real pages, so it
    runs on the worker, and the moderator arrives at a result that was produced
    minutes earlier.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        READING = "reading", "Reading the page"
        READY = "ready", "Ready to review"
        FAILED = "failed", "Couldn't read the page"
        APPLIED = "applied", "Applied"
        DISCARDED = "discarded", "Discarded"

    event = models.ForeignKey(
        Event, on_delete=models.CASCADE, related_name="refreshes"
    )
    # Copied at request time rather than read from the event when the worker
    # picks it up, so the record says which page was actually read even if
    # somebody edits the event's link in between.
    source_url = models.URLField()

    status = models.CharField(
        max_length=12, choices=Status.choices, default=Status.QUEUED
    )
    draft = models.JSONField(default=dict, blank=True)
    method = models.CharField(max_length=12, blank=True)
    message = models.CharField(max_length=300, blank=True)

    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="requested_refreshes",
    )
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="applied_refreshes",
    )
    # Which changes were taken, for the audit trail. A moderator who accepted
    # the dates and left the description alone should be readable as such
    # months later.
    applied_fields = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    objects = EventRefreshQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "created_at"])]
        verbose_name_plural = "event refreshes"

    def __str__(self):
        return f"{self.event.title} — {self.get_status_display()}"

    @property
    def is_running(self):
        return self.status in {self.Status.QUEUED, self.Status.READING}

    @property
    def method_label(self):
        """"Page's own event data" or "Language model", for the moderator.

        Worth showing: the free path is exact and the model's is a guess, and
        that changes how carefully the proposal below it deserves reading.
        """
        from enrichment.models import EnrichmentRun

        return dict(EnrichmentRun.Method.choices).get(self.method, "")

    @property
    def is_stranded(self):
        """Has this been claimed by a worker that died? See Submission."""
        # Imported here rather than at module scope: `events` is the domain
        # every other app reads, and importing a consumer from it at import
        # time would invert that and risk a cycle.
        from submissions.models import STALE_ENRICHMENT_AFTER

        if not self.is_running:
            return False
        return (timezone.now() - self.updated_at) > STALE_ENRICHMENT_AFTER

    def give_up(self):
        """Stop waiting on a worker that is not coming back."""
        self.status = self.Status.FAILED
        self.message = (
            "We didn't finish reading that page. Try again, or edit the "
            "listing by hand."
        )
        self.finished_at = timezone.now()
        self.save(update_fields=["status", "message", "finished_at", "updated_at"])
