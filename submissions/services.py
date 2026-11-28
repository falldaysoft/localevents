"""Turning a confirmed draft into a real Event.

Kept out of the view so the same path can be reused by the moderation queue and
by tests without going through HTTP.
"""

from django.db import transaction
from django.utils import timezone

from events.models import Event
from events.services import organizer_for, set_occurrences, venue_for

from .models import ModerationAction, Submission


@transaction.atomic
def save_event_from_draft(submission, data):
    """Build — or rebuild — the Event a moderator will review.

    Rebuild matters as much as build. When a moderator asks a question the
    submission goes back to its owner with the event already created, and a
    second confirmation has to *update* that event. Creating a fresh one would
    leave the moderator's queue entry pointing at an abandoned row and the
    answer to their question sitting in an event nobody is looking at.

    Status is PENDING, never PUBLISHED: the submitter's confirmation says the
    details are right, not that the event belongs on the site. Prominence is
    never touched here at all, because placement is a moderator's call.
    """
    venue = venue_for(
        data.get("venue_name", ""),
        data.get("venue_address", ""),
        data.get("venue_city", ""),
    )
    organizer = organizer_for(data.get("organizer_name", ""))

    event = submission.event or Event(
        source=Event.Source.SUBMISSION,
        submitted_by=submission.submitted_by,
    )
    is_new = event.pk is None

    event.title = data["title"]
    event.summary = data.get("summary", "")
    event.description = data.get("description", "")
    event.venue = venue
    event.organizer = organizer
    event.source_url = data.get("source_url", "") or submission.source_url
    event.ticket_url = data.get("ticket_url", "")
    event.is_free = data.get("is_free", False)
    event.price_note = data.get("price_note", "")
    event.is_family_friendly = data.get("is_family_friendly", False)
    event.accessibility_notes = data.get("accessibility_notes", "")
    event.listing_type = (
        Event.ListingType.SERIES
        if data.get("is_series")
        else Event.ListingType.ONE_OFF
    )
    event.status = Event.Status.PENDING

    rows = data.get("occurrences") or []
    if not event.slug:
        # Before the save, not after: the slug is permanent, so a one-off has
        # to know its year while there is still no slug to break. A
        # resubmission already has one and keeps it, whatever its dates now
        # say — someone has shared that link.
        event.assign_slug(min((row["start"] for row in rows), default=None))
    event.save()

    event.categories.set(data.get("categories") or [])

    # Every date carries its own hours. An earlier version wrote `end` onto the
    # first occurrence and null onto the rest, which silently threw away the
    # closing time of every date but one — a market open Fridays 9–2 and
    # Saturdays 7–2 published as "Saturday, 7am" and nothing more.
    set_occurrences(event, data.get("occurrences") or [])

    submission.event = event
    submission.status = Submission.Status.PENDING_REVIEW
    submission.save(update_fields=["event", "status", "updated_at"])

    ModerationAction.record(
        submission.submitted_by,
        "submitted_for_review" if is_new else "resubmitted_for_review",
        submission=submission,
        event=event,
        detail=event.title,
    )

    # A moderator's agent can send forty listings in one scouting run, and its
    # owner is already watching the queue, so those arrive without mail.
    if not submission.api_token_id:
        _mail_moderators(submission, "new" if is_new else "resubmitted")

    return event


def _mail_moderators(submission, kind):
    """Queue the "something to review" mail, once the save has committed.

    Imported here because `moderation` reads this app's models, not the other
    way round; on commit so a confirmation that rolls back mails nobody.
    """
    from moderation.tasks import email_moderators

    transaction.on_commit(lambda: email_moderators.enqueue(submission.pk, kind))


class CannotWithdraw(Exception):
    """Raised when a submission is past the point its owner can take it back.

    Once a moderator has decided, the submission is a record of that decision.
    An approved listing is removed through the edit/report path, where a
    moderator sees the request, rather than vanishing at a click.
    """


@transaction.atomic
def withdraw(submission, user, *, reason=""):
    """Take an open submission back, before any moderator has decided on it.

    The submission row is locked and re-read first: a moderator pressing
    Approve while the submitter presses Withdraw must leave one decision
    standing, not a withdrawn submission behind a published event.

    The event, if the submitter got that far, goes back to DRAFT rather than
    REJECTED. Nobody judged it; it simply never became a listing, and DRAFT
    keeps it out of every public query, the duplicate check and the
    moderators' Removed view, which is for listings that were taken down.
    """
    locked = Submission.objects.select_for_update().get(pk=submission.pk)
    if not locked.is_open:
        raise CannotWithdraw(
            f"This submission is already {locked.get_status_display().lower()}, "
            "so it can no longer be withdrawn."
            + (
                " To have the listing changed or taken down, report a problem "
                "on its page."
                if locked.status == Submission.Status.APPROVED
                else ""
            )
        )

    event = locked.event
    if event is not None:
        if event.status == Event.Status.PUBLISHED:
            # A moderator can publish from the edit form without deciding the
            # submission; once it is public, removal is theirs to make.
            raise CannotWithdraw(
                "This listing is already on the site, so it can no longer be "
                "withdrawn. Report a problem on its page to have it taken down."
            )
        if event.status == Event.Status.PENDING:
            event.status = Event.Status.DRAFT
            event.save(update_fields=["status", "updated_at"])

    locked.status = Submission.Status.WITHDRAWN
    locked.decided_by = user
    locked.decided_at = timezone.now()
    locked.decision_note = reason.strip()[:2000]
    locked.save(
        update_fields=[
            "status", "decided_by", "decided_at", "decision_note", "updated_at",
        ]
    )

    ModerationAction.record(
        user,
        "withdrawn",
        submission=locked,
        event=event,
        detail=reason.strip() or locked.display_title,
    )

    # The caller's copy is what the view renders from.
    submission.refresh_from_db()
    return submission
