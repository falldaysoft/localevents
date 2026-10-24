"""What a moderator's decision actually does.

Every decision funnels through here rather than through a view, for three
reasons that are easy to get wrong one view at a time: the event and the
submission must move together, an audit row must be written whether or not
anyone remembers to, and the submitter must be told. A view that did two of
those three would look fine in review and leave someone waiting forever.
"""

from django.db import transaction
from django.db.models import Avg, Exists, F, Max, OuterRef, Q, Subquery
from django.utils import timezone

from events.models import Event, EventRefresh, Interest, Occurrence, occurrence_overlaps
from submissions.models import ModerationAction, Submission, SubmissionMessage

from .tasks import email_submitter


class NothingToApprove(Exception):
    """Raised when a submission has no event behind it yet.

    Happens when a submitter starts a submission and never finishes the
    confirmation step. There is nothing to publish, so approving is a mistake
    rather than a decision.
    """


def assign(submission, moderator):
    """Take the submission, or hand it back.

    Assignment is advisory — it stops two volunteers writing to the same
    submitter, and nothing more. It never blocks anyone else from acting,
    because a queue that can be locked by someone who then goes on holiday is
    worse than one that occasionally doubles up.
    """
    submission.assigned_to = moderator
    submission.save(update_fields=["assigned_to", "updated_at"])
    ModerationAction.record(moderator, "assigned", submission=submission)
    return submission


def unassign(submission, moderator):
    submission.assigned_to = None
    submission.save(update_fields=["assigned_to", "updated_at"])
    ModerationAction.record(moderator, "unassigned", submission=submission)
    return submission


@transaction.atomic
def approve(submission, moderator, *, prominence, listing_type, categories=None, note=""):
    """Publish the event, at the prominence the moderator chose.

    Prominence and listing type are both set here rather than taken from the
    submission, because both are editorial judgements the submitter is not in a
    position to make. The submitter said "this repeats"; whether that means a
    collapsed series card or fifty individual listings is not their call, and
    neither is whether it belongs on the front page.
    """
    event = submission.event
    if event is None:
        raise NothingToApprove(
            "This submission has no event yet — the submitter never finished "
            "the details."
        )

    event.prominence = prominence
    event.listing_type = listing_type
    event.status = Event.Status.PUBLISHED
    # A full save, not update_fields: Event.save() derives published_at and a
    # series' renewal_token, and a partial save would compute them and then
    # quietly drop them.
    event.save()

    if categories is not None:
        event.categories.set(categories)

    submission.mark_decided(moderator, Submission.Status.APPROVED, note)

    ModerationAction.record(
        moderator,
        "approved",
        submission=submission,
        event=event,
        detail=f"{event.get_prominence_display()} — {event.title}",
    )
    # A moderator approving their own listing needs no mail announcing what
    # they just did. The audit row above is still written.
    if submission.submitted_by_id != moderator.pk:
        _notify(submission, "approved")
    return event


class NotQuickApprovable(Exception):
    """Raised when a submission is not one the one-click Approve may take.

    The queue's Approve button skips the review screen, so it is offered only
    where the review already happened somewhere else: a submission sent by a
    moderator's own agent, which read the page on that moderator's behalf.
    A stranger's submission still goes through the full screen, where the
    moderator sees the page, the dates and the duplicates before deciding.
    """


class AlreadyDecided(Exception):
    """Raised when a decision has already been made on this submission.

    A one-click button on a list invites a double click, and the second one
    must not write a second audit row or send a second email.
    """


def quick_approve(submission, moderator, tier=None):
    """Publish an agent's submission from its queue row, with no form between.

    The one editorial call a moderator makes on approval is the tier, so the
    row offers that and nothing else: `tier` is the button they pressed, or
    the drafted prominence when the row has no picker. Listing type and
    categories stay as drafted — the full screen is one click away for
    anything that needs a different answer.
    """
    if submission.api_token_id is None:
        raise NotQuickApprovable(
            "Only a listing sent by an agent can be approved from the queue."
        )
    if submission.status != Submission.Status.PENDING_REVIEW:
        raise AlreadyDecided(
            f"This submission is already {submission.get_status_display().lower()}."
        )
    event = submission.event
    if event is None:
        raise NothingToApprove("This submission has no event yet.")
    return approve(
        submission,
        moderator,
        prominence=Event.Prominence(tier) if tier is not None else event.prominence,
        listing_type=event.listing_type,
    )


@transaction.atomic
def reject(submission, moderator, *, reason):
    """Decline the listing, with a reason the submitter will read.

    The reason is mandatory. A bare rejection from a community site reads as
    arbitrary, and the person on the other end is usually a volunteer who spent
    ten minutes filling in a form.
    """
    event = submission.event
    if event is not None:
        event.status = Event.Status.REJECTED
        event.save(update_fields=["status", "updated_at"])

    submission.mark_decided(moderator, Submission.Status.REJECTED, reason)

    ModerationAction.record(
        moderator,
        "rejected",
        submission=submission,
        event=event,
        detail=reason,
    )
    _notify(submission, "rejected")
    return submission


@transaction.atomic
def request_info(submission, moderator, *, body):
    """Ask the submitter a question and hand the submission back to them.

    Deliberately not a rejection. Most of what reaches the queue is nearly
    right — a missing address, an ambiguous date — and asking costs far less
    than making someone start again.
    """
    message = SubmissionMessage.objects.create(
        submission=submission,
        author=moderator,
        body=body,
        is_from_moderator=True,
    )

    submission.status = Submission.Status.INFO_REQUESTED
    submission.assigned_to = moderator
    submission.save(update_fields=["status", "assigned_to", "updated_at"])

    ModerationAction.record(
        moderator, "requested_info", submission=submission,
        event=submission.event, detail=body,
    )
    _notify(submission, "info_requested", message_id=message.pk)
    return message


def _notify(submission, kind, message_id=None):
    """Queue the submitter's email.

    On the queue rather than inline because SMTP is the slowest and least
    reliable thing in the request, and a moderator should not watch a spinner —
    or worse, see a decision fail to save — because a mail relay is down. The
    decision is already committed by the time this runs.
    """
    transaction.on_commit(
        lambda: email_submitter.enqueue(submission.pk, kind, message_id)
    )


# ---------------------------------------------------------------------------
# Rising
# ---------------------------------------------------------------------------


# A ratio is meaningless at tiny counts: promote the only busy event out of a
# tier and whatever is left becomes that tier's average, so the next event with
# two marks reads as "twice the average". Observed exactly that way in a live
# run. An absolute floor is the honest guard — two interested people is not a
# promotion case however the arithmetic flatters it.
MIN_INTEREST_TO_RISE = 5


def rising_events(limit=25, now=None):
    """Published events the crowd is pushing at, ranked *within* their tier.

    Comparing raw interest counts across tiers would only ever rediscover that
    featured events get more attention than background ones — they are on the
    front page. What is worth a moderator's time is an event doing unusually
    well *for where it currently sits*: the Tuesday knitting group with forty
    interested people is the promotion this queue exists to surface.

    Featured events are excluded because there is nowhere above them to go.

    This is a nomination, never a promotion. Nothing here changes prominence;
    a human clicks, which is the whole reason anonymous interest with weak
    deduplication is an acceptable signal in the first place.
    """
    now = now or timezone.now()

    averages = {
        row["prominence"]: row["mean"] or 0
        for row in Event.objects.published()
        .values("prominence")
        .annotate(mean=Avg("interest_count"))
    }

    candidates = (
        Event.objects.published()
        .filter(interest_count__gte=MIN_INTEREST_TO_RISE)
        .exclude(prominence=Event.Prominence.FEATURED)
        .filter(occurrences__start__gte=now, occurrences__is_cancelled=False)
        .select_related("venue", "organizer")
        .distinct()
    )

    ranked = []
    for event in candidates:
        mean = averages.get(event.prominence) or 0
        # A tier whose events average less than one interest would otherwise
        # divide by nearly zero and rank a single click above everything.
        event.tier_average = mean
        event.rising_score = event.interest_count / max(mean, 1.0)
        # Below its tier average an event is not rising, whatever its raw
        # count. Listing those under this heading would make the queue read as
        # "every published event", which is the thing it exists to avoid.
        if event.rising_score >= 1:
            ranked.append(event)

    ranked.sort(key=lambda e: (e.rising_score, e.interest_count), reverse=True)
    return ranked[:limit]


# A promotion means "onto the front page". Quiet is where a moderator puts a
# listing to keep it *off* the front page, not a rung a rising program should
# have to climb twice — so both of the lower tiers promote straight to Listed.
NEXT_TIER = {
    Event.Prominence.BACKGROUND: Event.Prominence.LISTED,
    Event.Prominence.QUIET: Event.Prominence.LISTED,
    Event.Prominence.LISTED: Event.Prominence.FEATURED,
}


def promote(event, moderator):
    """Move an event up exactly one tier, on a human's say-so."""
    target = NEXT_TIER.get(event.prominence)
    if target is None:
        return event

    was = event.get_prominence_display()
    event.prominence = target
    event.save(update_fields=["prominence", "updated_at"])

    ModerationAction.record(
        moderator,
        "promoted",
        event=event,
        detail=f"{was} → {event.get_prominence_display()} ({event.title})",
    )
    return event


# ---------------------------------------------------------------------------
# The events screen: every listing, and the quick decisions about one
# ---------------------------------------------------------------------------
#
# The queue judges a submission once. This is the other side of that: the
# hundred listings already on the site, which a moderator wants to re-tier,
# take down or vote for without opening each one. Each action below is one
# click on a row, so each is reversible and each writes an audit row — a
# quick button with no record is how "who took that down?" becomes a mystery.


REMOVED_STATUSES = [
    Event.Status.UNPUBLISHED,
    Event.Status.DUPLICATE,
    Event.Status.REJECTED,
    Event.Status.EXPIRED,
]

EVENT_VIEWS = {
    "upcoming": "Upcoming",
    "past": "Past",
    "pending": "Pending",
    "removed": "Removed",
    "all": "Everything",
}

EVENT_SORTS = {
    "date": "Next date",
    "title": "Title",
    "venue": "Venue",
    "tier": "Tier",
    "interest": "Interest",
    "published": "Recently published",
    "updated": "Recently changed",
}

# More than a screen's worth is a search waiting to be typed.
EVENT_LIST_CAP = 200

# The tier labels are written for a radio button with room to explain itself;
# a segmented control has room for one word. (value, short name, full label).
TIER_BUTTONS = [
    (p.value, p.label.split(" — ")[0], p.label) for p in Event.Prominence
]


def annotated_events(moderator, now=None):
    """Every event, with the dates a row needs to render.

    Not `EventFilter.apply`: that drops anything without an upcoming date,
    and this screen exists partly to find the listing whose dates ran out.
    `next_start` is the soonest live occurrence or null; `last_start` is the
    final date it ever had, for the past and removed rows.
    """
    now = now or timezone.now()
    soonest = (
        Occurrence.objects.filter(
            occurrence_overlaps(now), event=OuterRef("pk"), is_cancelled=False
        )
        .order_by("start")
        .values("start")[:1]
    )
    return (
        Event.objects.select_related("venue", "organizer")
        .annotate(
            next_start=Subquery(soonest),
            last_start=Max("occurrences__start"),
            moderator_interested=Exists(
                Interest.objects.filter(event=OuterRef("pk"), user=moderator)
            ),
        )
        .distinct()
    )


def event_list(moderator, *, view="upcoming", tier=None, query="", sort="date", now=None):
    """The rows behind one view of the events screen, capped."""
    now = now or timezone.now()
    events = annotated_events(moderator, now)

    if view == "upcoming":
        events = events.filter(status=Event.Status.PUBLISHED, next_start__isnull=False)
    elif view == "past":
        events = events.filter(status=Event.Status.PUBLISHED, next_start__isnull=True)
    elif view == "pending":
        events = events.filter(status=Event.Status.PENDING)
    elif view == "removed":
        events = events.filter(status__in=REMOVED_STATUSES)

    if tier is not None:
        events = events.filter(prominence=tier)
    if query:
        events = events.filter(
            Q(title__icontains=query)
            | Q(venue__name__icontains=query)
            | Q(organizer__name__icontains=query)
        )

    ordering = {
        "date": [F("next_start").asc(nulls_last=True), "-last_start", "title"],
        "title": ["title"],
        "venue": ["venue__name", F("next_start").asc(nulls_last=True)],
        "tier": ["-prominence", F("next_start").asc(nulls_last=True)],
        "interest": ["-interest_count", F("next_start").asc(nulls_last=True)],
        "published": ["-published_at", "-pk"],
        "updated": ["-updated_at"],
    }[sort if sort in EVENT_SORTS else "date"]
    return events.order_by(*ordering)


def set_tier(event, moderator, tier):
    """Put a listing in a tier, on a human's say-so, and say who did."""
    tier = Event.Prominence(tier)
    if event.prominence == tier:
        return event
    was = event.get_prominence_display()
    event.prominence = tier
    event.save(update_fields=["prominence", "updated_at"])
    ModerationAction.record(
        moderator,
        "tier_changed",
        event=event,
        detail=f"{was} → {event.get_prominence_display()} ({event.title})",
    )
    return event


def unpublish(event, moderator, *, duplicate=False):
    """Take a listing off the site. Reversible with `republish`.

    No mail to the submitter, matching the edit form's status field, which
    has always unpublished silently: the listing is still theirs to see at
    its submission page, and a duplicate removal is housekeeping, not a
    judgement on them.
    """
    if event.status in REMOVED_STATUSES:
        return event
    event.status = Event.Status.DUPLICATE if duplicate else Event.Status.UNPUBLISHED
    event.save(update_fields=["status", "updated_at"])
    ModerationAction.record(
        moderator,
        "marked_duplicate" if duplicate else "unpublished",
        event=event,
        detail=event.title,
    )
    return event


def republish(event, moderator):
    """Put a removed listing back exactly as it was."""
    if event.status == Event.Status.PUBLISHED:
        return event
    was = event.get_status_display()
    event.status = Event.Status.PUBLISHED
    # A full save: a listing removed before it was ever published still needs
    # its published_at derived.
    event.save()
    ModerationAction.record(
        moderator, "republished", event=event, detail=f"was {was} ({event.title})"
    )
    return event


def toggle_interest(event, user):
    """The moderator's own "Interested" mark — the reader's star, from the
    events screen. See `events.services.toggle_interest` for why it is not an
    audit event. Returns whether the user is now interested."""
    from events.services import toggle_interest as toggle

    return toggle(event, user=user)


# ---------------------------------------------------------------------------
# The queue itself
# ---------------------------------------------------------------------------

QUEUE_VIEWS = {
    "review": "Awaiting review",
    "mine": "Assigned to me",
    "waiting": "Waiting on submitters",
    "open": "Everything open",
    "decided": "Decided",
}


def queue_for(view, moderator):
    """The submissions behind one tab of the queue."""
    base = Submission.objects.select_related(
        "submitted_by", "assigned_to", "api_token", "event", "event__venue"
    )

    if view == "mine":
        return base.filter(assigned_to=moderator).filter(
            status__in=[
                Submission.Status.PENDING_REVIEW,
                Submission.Status.INFO_REQUESTED,
            ]
        )
    if view == "waiting":
        return base.filter(
            status__in=[
                Submission.Status.INFO_REQUESTED,
                Submission.Status.AWAITING_SUBMITTER,
            ]
        )
    if view == "open":
        return base.exclude(
            status__in=[
                Submission.Status.APPROVED,
                Submission.Status.REJECTED,
                Submission.Status.WITHDRAWN,
            ]
        )
    if view == "decided":
        return base.filter(
            status__in=[
                Submission.Status.APPROVED,
                Submission.Status.REJECTED,
            ]
        ).order_by("-decided_at")

    return base.awaiting_review().order_by("created_at")


def queue_counts(moderator):
    """Badge numbers for the tabs.

    One grouped query rather than five counts, because this renders on every
    page of the queue.
    """
    rows = Submission.objects.aggregate(
        review=_count(status=Submission.Status.PENDING_REVIEW),
        mine=_count(
            assigned_to=moderator,
            status__in=[
                Submission.Status.PENDING_REVIEW,
                Submission.Status.INFO_REQUESTED,
            ],
        ),
        waiting=_count(
            status__in=[
                Submission.Status.INFO_REQUESTED,
                Submission.Status.AWAITING_SUBMITTER,
            ]
        ),
    )
    # A second query, because refreshes hang off events rather than
    # submissions. Worth it: a bulk re-read queued from the admin lands
    # nowhere a moderator would think to look, and an unbadged tab is a
    # feature nobody uses.
    rows["refreshes"] = EventRefresh.objects.awaiting_review().count()
    return rows


def _count(**lookups):
    from django.db.models import Count

    return Count("pk", filter=Q(**lookups))
