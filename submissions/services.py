"""Turning a confirmed draft into a real Event.

Kept out of the view so the same path can be reused by the moderation queue and
by tests without going through HTTP.
"""

from django.db import transaction

from events.geocoding import geocode_venue
from events.models import Event, Occurrence, Organizer, Venue

from .models import ModerationAction, Submission


def _venue_for(name, address, city):
    """Find or create a venue, queueing geocoding only for genuinely new ones.

    Matching on name and city rather than creating blindly is what keeps the
    map useful — three events at the community hall should share one pin, and
    one geocode.
    """
    if not name.strip():
        return None

    venue = Venue.objects.filter(name__iexact=name.strip(), city__iexact=city.strip()).first()
    if venue:
        # Fill in an address we didn't have before; don't overwrite a good one.
        if address and not venue.address:
            venue.address = address
            venue.save(update_fields=["address"])
        return venue

    venue = Venue.objects.create(
        name=name.strip()[:200], address=address.strip()[:300], city=city.strip()[:120]
    )
    geocode_venue.enqueue(venue.pk)
    return venue


def _organizer_for(name):
    if not name.strip():
        return None
    organizer = Organizer.objects.filter(name__iexact=name.strip()).first()
    if organizer:
        return organizer
    return Organizer.objects.create(name=name.strip()[:200])


@transaction.atomic
def create_event_from_draft(submission, data):
    """Build the Event a moderator will review.

    Status is PENDING, never PUBLISHED: the submitter's confirmation says the
    details are right, not that the event belongs on the site. Prominence stays
    at the default because placement is a moderator's call.
    """
    venue = _venue_for(
        data.get("venue_name", ""),
        data.get("venue_address", ""),
        data.get("venue_city", ""),
    )
    organizer = _organizer_for(data.get("organizer_name", ""))

    event = Event.objects.create(
        title=data["title"],
        summary=data.get("summary", ""),
        description=data.get("description", ""),
        venue=venue,
        organizer=organizer,
        source_url=data.get("source_url", "") or submission.source_url,
        ticket_url=data.get("ticket_url", ""),
        is_free=data.get("is_free", False),
        price_note=data.get("price_note", ""),
        is_family_friendly=data.get("is_family_friendly", False),
        accessibility_notes=data.get("accessibility_notes", ""),
        listing_type=(
            Event.ListingType.SERIES
            if data.get("is_series")
            else Event.ListingType.ONE_OFF
        ),
        status=Event.Status.PENDING,
        source=Event.Source.SUBMISSION,
        submitted_by=submission.submitted_by,
    )

    if data.get("categories"):
        event.categories.set(data["categories"])

    starts = [data["starts_at"], *data.get("additional_dates", [])]
    for index, start in enumerate(starts):
        Occurrence.objects.get_or_create(
            event=event,
            start=start,
            defaults={"end": data.get("ends_at") if index == 0 else None},
        )

    submission.event = event
    submission.status = Submission.Status.PENDING_REVIEW
    submission.save(update_fields=["event", "status", "updated_at"])

    ModerationAction.record(
        submission.submitted_by,
        "submitted_for_review",
        submission=submission,
        event=event,
        detail=event.title,
    )

    return event
