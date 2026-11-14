"""What goes in one reader's weekly email.

Two halves. The *base* is the next seven days, built by the same
`web.listing._listing` the front page uses, so a weekly series appears once,
Featured sits above the days and Quiet listings are folded under theirs — an
email that drifted from the site would list a weekly class seven times. The
*reader's* half is laid over it: the events they starred that are on this
weekend go first under their own heading, and wherever a starred event
appears further down it carries a star, so meeting it twice reads as a
reminder rather than a mistake.

The base depends only on the filter, so a sender handling many readers can
build it once per filter and pass it to `render_for` (see `base_listing`).
Today every reader has the same filter; per-reader categories
(localevents.14.6) arrive as a different `EventFilter` and nothing else here
changes.
"""

import re
from dataclasses import dataclass, field

from django.conf import settings
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from core.models import SiteConfig
from events.models import Event, Interest
from web.filters import EventFilter
from web.listing import _listing

# An email is read top to bottom once, not paged; past about this many cards
# a reader has stopped and the rest is weight. Whatever is cut is counted and
# linked, never silently dropped.
MAX_CARDS = 40
# Standing weekly programs get one line each, and a short list of them.
MAX_PROGRAMS = 12


@dataclass
class Digest:
    subject: str
    text: str
    html: str
    # What a send log wants to count: listings in this email, and how many of
    # them led it as the reader's own weekend picks.
    event_count: int
    favorites_count: int
    event_ids: list = field(default_factory=list)


def absolute_url(path):
    return f"{settings.SITE_BASE_URL.rstrip('/')}{path}"


def default_filter():
    return EventFilter(when="week")


def filter_for(subscriber):
    """The reader's own question. Everyone asks the same one, for now."""
    return default_filter()


def base_listing(filters, now):
    """The seven days for `filters`, capped for an email.

    Quiet listings are folded under their day as on the front page — the
    site's own horizon is longer than a week, so every Quiet listing in the
    window is included, as a line rather than a card. Cut days are counted in
    `more_count` so the email can say what it left out.
    """
    listing = _listing(
        filters,
        now,
        quiet_horizon_days=SiteConfig.load().quiet_horizon_days,
        paged=False,
    )
    featured = listing["featured"]
    budget = max(MAX_CARDS - len(featured), 0)
    days, shown, more = [], 0, 0
    for day in listing["days"]:
        if shown >= budget:
            more += len(day["events"]) + len(day["quiet"])
            continue
        room = budget - shown
        kept = day["events"][:room]
        more += len(day["events"]) - len(kept)
        shown += len(kept)
        days.append({**day, "events": kept})
    programs = listing["programs"]
    return {
        "featured": featured,
        "days": days,
        "programs": programs[:MAX_PROGRAMS],
        "more_programs": max(len(programs) - MAX_PROGRAMS, 0),
        "more_count": more,
    }


def marked_ids(subscriber):
    """The events this reader has starred, by account or by device."""
    if subscriber.user_id:
        rows = Interest.objects.filter(user_id=subscriber.user_id)
    elif subscriber.fingerprint:
        rows = Interest.objects.filter(
            user__isnull=True, fingerprint=subscriber.fingerprint
        )
    else:
        return set()
    return set(rows.values_list("event_id", flat=True))


def weekend_favorites(ids, now):
    """Starred events on this coming weekend, in any tier.

    Any tier, because the reader chose them: a Quiet talk or a weekly class
    they starred is exactly what they want reminded of, even though neither
    would earn a card of its own. "Weekend" is the browse filter's — on a
    Saturday it means today and tomorrow, not next week's.
    """
    if not ids:
        return []
    filters = EventFilter(when="weekend", ids=ids)
    return list(filters.apply(Event.objects.published(), now).order_by("next_start"))


def _event_ids(base, favorites):
    ids = [e.pk for e in favorites]
    ids += [e.pk for e in base["featured"]]
    for day in base["days"]:
        ids += [e.pk for e in day["events"]] + [e.pk for e in day["quiet"]]
    ids += [e.pk for e in base["programs"]]
    return list(dict.fromkeys(ids))


def _subject(favorites, base):
    site = settings.SITE_NAME
    if favorites:
        first = favorites[0].title
        if len(favorites) == 1:
            return f"This weekend: {first} — and what's on at {site}"
        return f"This weekend: {first} and {len(favorites) - 1} more of yours — {site}"
    if base["featured"]:
        return f"What's on this week: {base['featured'][0].title} and more — {site}"
    return f"What's on this week — {site}"


def render_for(subscriber, now=None, base=None, unsubscribe_url=None):
    """The whole email for one reader, ready to send.

    `base` may be passed in by a sender that has already built it for this
    reader's filter; it is built here otherwise. `unsubscribe_url` is the
    sender's to choose, since it may want to say which email it came from.
    """
    now = now or timezone.now()
    if base is None:
        base = base_listing(filter_for(subscriber), now)
    marked = marked_ids(subscriber)
    favorites = weekend_favorites(marked, now)
    event_ids = _event_ids(base, favorites)

    site_config = SiteConfig.load()
    context = {
        **base,
        "favorites": favorites,
        "marked": marked,
        "now": now,
        "site_name": settings.SITE_NAME,
        "site_url": absolute_url("/"),
        "week_url": absolute_url(reverse("index") + "?when=week"),
        "signup_url": absolute_url(reverse("digest_signup")),
        "unsubscribe_url": unsubscribe_url
        or absolute_url(reverse("digest_unsubscribe", args=[subscriber.token])),
        "contact_email": settings.CONTACT_EMAIL,
        "postal_address": site_config.postal_address.strip(),
        "base_url": settings.SITE_BASE_URL.rstrip("/"),
    }
    return Digest(
        subject=_subject(favorites, base),
        # The text template is written for legibility, not for whitespace;
        # tags leave blank runs that a plain-text reader should never see.
        text=re.sub(
            r"\n{3,}", "\n\n", render_to_string("digest/email/digest.txt", context)
        ).strip()
        + "\n",
        html=render_to_string("digest/email/digest.html", context),
        event_count=len(event_ids),
        favorites_count=len(favorites),
        event_ids=event_ids,
    )


def render_default(now=None):
    """The email a reader with no stars gets this week: the run's record.

    Its unsubscribe link goes to the signup page, as a test copy to a
    non-subscriber does, because a stored copy belongs to nobody.
    """
    from .models import Subscriber

    return render_for(
        Subscriber(), now=now, unsubscribe_url=absolute_url(reverse("digest_signup"))
    )
