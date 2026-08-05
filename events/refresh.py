"""Reading an event's source page again, and offering a human what changed.

Two things go stale after a listing is approved. The page moves — a time
changes, dates are added, a venue is corrected — and so does our reading of
it: an event entered before occurrences carried their own end times has one
date where the page always listed six, and nothing but another read of the
page will recover them.

The whole design is in what this module does *not* do. It never writes to an
event by itself. It produces a proposal, field by field, with the current
value beside the new one, and a moderator ticks what to take. Auto-applying
would put an extraction — the same extraction that got the year wrong twice in
live testing — straight onto a published listing that a human had already
checked, and the first anyone would hear of it is a reader turning up on the
wrong evening.

Three further rules fall out of that, and each has cost something somewhere:

- A refresh never proposes *emptying* a field. A page that has been redesigned
  into a JavaScript shell extracts as a title and nothing else, and treating
  that silence as "the description is gone now" would gut good listings.
- A refresh never touches placement — status, prominence, listing type, slug.
  Those are editorial decisions and a page has no opinion about them.
- A refresh only replaces dates from today onward. A page describes what is
  coming; it says nothing about the eight months this class has already run.
"""

import logging

from django.db import transaction
from django.tasks import task
from django.utils import timezone

from events.models import Category, EventRefresh
from events.services import organizer_for, set_occurrences, venue_for
from events.templatetags.event_dates import occurrence_when

logger = logging.getLogger("events.refresh")


# ---------------------------------------------------------------------------
# Doing the read
# ---------------------------------------------------------------------------


def request_refresh(event, user=None):
    """Queue a re-read of this event's source page.

    Returns the existing run if one is already in flight. Two moderators
    opening the same listing on a Sunday evening should not buy two model
    calls, and the second would only overwrite the first's answer.
    """
    running = event.refreshes.filter(
        status__in=[EventRefresh.Status.QUEUED, EventRefresh.Status.READING]
    ).first()
    if running is not None and not running.is_stranded:
        return running
    if running is not None:
        running.give_up()

    # An older proposal nobody acted on is superseded, not left beside the new
    # one. Two rows for the same event in the refresh queue is a moderator
    # comparing a listing against two different readings of the same page.
    event.refreshes.filter(status=EventRefresh.Status.READY).update(
        status=EventRefresh.Status.DISCARDED
    )

    refresh = EventRefresh.objects.create(
        event=event, source_url=event.source_url, requested_by=user
    )
    refresh_event_from_source.enqueue(refresh.pk)
    return refresh


@task()
def refresh_event_from_source(refresh_id):
    """Read the page and park the result for a moderator.

    Never raises, for the same reason the submission enrichment task doesn't:
    a crash here would leave the refresh claimed forever and the moderator
    watching a spinner with no way to tell whether anything is still happening.
    """
    from enrichment.pipeline import enrich_url

    try:
        refresh = EventRefresh.objects.select_related("event").get(pk=refresh_id)
    except EventRefresh.DoesNotExist:
        logger.warning("refresh_event_from_source: %s no longer exists", refresh_id)
        return

    if refresh.status != EventRefresh.Status.QUEUED:
        return

    refresh.status = EventRefresh.Status.READING
    refresh.save(update_fields=["status", "updated_at"])

    try:
        result = enrich_url(refresh.source_url, event=refresh.event)
    except Exception:
        logger.exception("refresh crashed for %s", refresh_id)
        refresh.status = EventRefresh.Status.FAILED
        refresh.message = "Something went wrong reading that page."
        refresh.finished_at = timezone.now()
        refresh.save()
        return

    refresh.draft = result.draft.model_dump(mode="json") if result.draft else {}
    refresh.method = result.method or ""
    refresh.message = result.message[:300]
    refresh.status = (
        EventRefresh.Status.READY if result.succeeded else EventRefresh.Status.FAILED
    )
    refresh.finished_at = timezone.now()
    refresh.save()


# ---------------------------------------------------------------------------
# What changed
# ---------------------------------------------------------------------------


class Change:
    """One field the page now disagrees with, and how to take it.

    The value and the way to apply it travel together deliberately. An earlier
    shape had the diff list the fields and a separate function write them,
    keyed by name — which is two lists to keep in step, and the day they drift
    a moderator ticks "dates" and gets a description.
    """

    def __init__(self, key, label, current, proposed, apply, warning=""):
        self.key = key
        self.label = label
        self.current = current  # list of display lines
        self.proposed = proposed  # list of display lines
        self.apply = apply  # apply(event) -> None
        self.warning = warning


# Fields where the page's text simply replaces ours. Placement, pricing
# numbers and the source link itself are all absent: the first is editorial,
# the second interacts with `is_free` in ways only the edit form validates,
# and the third is the thing we just read.
TEXT_FIELDS = (
    ("title", "Title"),
    ("summary", "Summary"),
    ("description", "Description"),
    ("price_note", "Price note"),
    ("accessibility_notes", "Accessibility notes"),
    ("ticket_url", "Ticket link"),
    ("image_url", "Image link"),
)

# Flags are only ever proposed when the page says *yes*. The extraction schema
# defaults them to False, so a False is indistinguishable from "the page did
# not mention it" — and acting on that would quietly un-free a free event
# every time a page was rewritten.
FLAG_FIELDS = (
    ("is_free", "Free to attend"),
    ("is_family_friendly", "Family friendly"),
)


def _draft_of(refresh):
    """The stored draft back as an EventDraft, or None if it won't parse."""
    from enrichment.schemas import EventDraft

    if not refresh.draft:
        return None
    try:
        return EventDraft.model_validate(refresh.draft)
    except Exception:
        # A draft written by an older schema is not worth crashing a queue
        # page over; the moderator can read the page themselves.
        logger.warning("refresh %s has an unreadable draft", refresh.pk)
        return None


def _aware(value):
    if value is None:
        return None
    if timezone.is_naive(value):
        # The model is told to report local time, so a naive value is local.
        return timezone.make_aware(value)
    return value


def _setter(field, value):
    def apply(event):
        setattr(event, field, value)

    return apply


def changes_for(refresh):
    """Every field the re-read disagrees with, as a list of Changes.

    Only differences appear. A refresh that found the page unchanged returns
    an empty list, which is a perfectly good answer and the one a moderator
    most wants to hear.
    """
    draft = _draft_of(refresh)
    if draft is None:
        return []

    event = refresh.event
    changes = []

    for field, label in TEXT_FIELDS:
        proposed = (getattr(draft, field, "") or "").strip()
        current = (getattr(event, field, "") or "").strip()
        if proposed and proposed != current:
            changes.append(
                Change(
                    field,
                    label,
                    [current] if current else ["— nothing on record —"],
                    [proposed],
                    _setter(field, proposed),
                )
            )

    for field, label in FLAG_FIELDS:
        if not getattr(draft, field, False) or getattr(event, field, False):
            continue
        # Marking something free that carries a price is the contradiction the
        # edit form rejects; don't create it from behind a checkbox.
        if field == "is_free" and (
            event.price_min is not None or event.price_max is not None
        ):
            continue
        changes.append(
            Change(field, label, ["No"], ["Yes"], _setter(field, True))
        )

    venue = _venue_change(event, draft)
    if venue is not None:
        changes.append(venue)

    organizer = _organizer_change(event, draft)
    if organizer is not None:
        changes.append(organizer)

    categories = _category_change(event, draft)
    if categories is not None:
        changes.append(categories)

    dates = _date_change(event, draft)
    if dates is not None:
        changes.append(dates)

    return changes


def _venue_change(event, draft):
    name = (draft.venue_name or "").strip()
    if not name:
        return None

    city = (draft.venue_city or "").strip()
    address = (draft.venue_address or "").strip()
    current = event.venue

    same = (
        current is not None
        and current.name.casefold() == name.casefold()
        and current.city.casefold() == city.casefold()
    )
    if same:
        return None

    def apply(event):
        event.venue = venue_for(name, address, city)

    return Change(
        "venue",
        "Venue",
        [str(current)] if current else ["— nothing on record —"],
        [", ".join(part for part in (name, address, city) if part)],
        apply,
    )


def _organizer_change(event, draft):
    name = (draft.organizer_name or "").strip()
    if not name:
        return None
    current = event.organizer
    if current is not None and current.name.casefold() == name.casefold():
        return None

    def apply(event):
        event.organizer = organizer_for(name)

    return Change(
        "organizer",
        "Organizer",
        [current.name] if current else ["— nothing on record —"],
        [name],
        apply,
    )


def _category_change(event, draft):
    """Categories, only ever added to.

    A moderator's categorisation is a judgement made with the whole site in
    view, and an extraction that recognised one of the slugs it was shown is
    not grounds for discarding the other two. So the proposal is the union,
    and it only appears when the page genuinely contributes something.
    """
    slugs = [slug for slug in (draft.category_slugs or []) if slug]
    if not slugs:
        return None

    found = list(Category.objects.filter(slug__in=slugs, is_active=True))
    current = list(event.categories.all())
    extra = [c for c in found if c not in current]
    if not extra:
        return None

    def apply(event):
        event.categories.add(*extra)

    return Change(
        "categories",
        "Categories",
        [c.name for c in current] or ["— none —"],
        [c.name for c in current + extra],
        apply,
    )


def _date_change(event, draft):
    """The dates, compared from today onward.

    This is the change the whole feature exists for. An event listed before
    occurrences carried their own hours has one date and a page that has
    always shown six; nothing else on the listing needs fixing.
    """
    rows = [
        {
            "start": _aware(occurrence.start),
            "end": _aware(occurrence.end),
            "note": (occurrence.note or "")[:200],
        }
        for occurrence in (draft.occurrences or [])
        if occurrence.start is not None
    ]
    rows.sort(key=lambda row: row["start"])
    if not rows:
        return None

    now = timezone.now()
    current = list(event.occurrences.filter(start__gte=now).order_by("start"))

    if [(o.start, o.end) for o in current] == [
        (row["start"], row["end"]) for row in rows
    ]:
        return None

    def apply(event):
        set_occurrences(event, rows, keep_before=now)

    # The model's standing habit is to date an undated page to last year. The
    # submitter's formset refuses a set that has entirely gone by; a moderator
    # gets told instead, because they may be looking at a genuinely finished
    # series and are the ones able to tell the difference.
    warning = ""
    if max(row["start"] for row in rows) < now:
        warning = (
            "Every date found is in the past. Check the year on the page "
            "before taking these — a page that gives a day and month with no "
            "year is the case extraction gets wrong."
        )

    return Change(
        "occurrences",
        "Dates",
        [occurrence_when(o.start, o.end, with_year=True) for o in current]
        or ["— nothing upcoming —"],
        [occurrence_when(row["start"], row["end"], with_year=True) for row in rows],
        apply,
        warning=warning,
    )


# ---------------------------------------------------------------------------
# Taking it
# ---------------------------------------------------------------------------


@transaction.atomic
def apply_refresh(refresh, keys, moderator=None):
    """Write the ticked changes onto the event.

    Returns the labels applied, for the caller to put in the audit trail.
    Nothing outside the ticked set moves, and neither does the event's status
    or prominence: a refresh corrects what a listing *says*, never where it
    sits or whether it is up.
    """
    event = refresh.event
    taken = [change for change in changes_for(refresh) if change.key in keys]

    for change in taken:
        change.apply(event)

    # A full save, not update_fields: Event.save() derives published_at and a
    # series' renewal token, and the set of touched fields varies per refresh.
    event.save()

    refresh.status = EventRefresh.Status.APPLIED
    refresh.applied_by = moderator
    refresh.applied_fields = [change.label for change in taken]
    refresh.save(update_fields=["status", "applied_by", "applied_fields", "updated_at"])

    return refresh.applied_fields


def discard_refresh(refresh, moderator=None):
    """Say no to the whole proposal, and stop it showing in the queue."""
    refresh.status = EventRefresh.Status.DISCARDED
    refresh.applied_by = moderator
    refresh.save(update_fields=["status", "applied_by", "updated_at"])
    return refresh
