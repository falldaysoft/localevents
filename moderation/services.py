"""What a moderator's decision actually does.

Every decision funnels through here rather than through a view, for three
reasons that are easy to get wrong one view at a time: the event and the
submission must move together, an audit row must be written whether or not
anyone remembers to, and the submitter must be told. A view that did two of
those three would look fine in review and leave someone waiting forever.
"""

from django.db import transaction
from django.db.models import Avg, Q
from django.utils import timezone

from events.models import Event, EventRefresh
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
    _notify(submission, "approved")
    return event


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


NEXT_TIER = {
    Event.Prominence.BACKGROUND: Event.Prominence.LISTED,
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
        "submitted_by", "assigned_to", "event", "event__venue"
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
