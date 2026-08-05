"""Writes to the domain that more than one path has to make the same way.

Three callers now build the same records — a submitter confirming a draft, a
moderator editing dates, and a refresh applying what a source page says today.
All three need "find this venue or make it" and "these are the dates now" to
behave identically. When only the submission path had them, the map grew a
second pin the moment another path created a venue the first would have
reused, and there was nothing in the code to say the two should agree.
"""

from events.geocoding import geocode_venue
from events.models import Occurrence, Organizer, Venue


def venue_for(name, address, city):
    """Find or create a venue, queueing geocoding only for genuinely new ones.

    Matching on name and city rather than creating blindly is what keeps the
    map useful — three events at the community hall should share one pin, and
    one geocode.
    """
    if not name.strip():
        return None

    venue = Venue.objects.filter(
        name__iexact=name.strip(), city__iexact=city.strip()
    ).first()
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


def organizer_for(name):
    if not name.strip():
        return None
    organizer = Organizer.objects.filter(name__iexact=name.strip()).first()
    if organizer:
        return organizer
    return Organizer.objects.create(name=name.strip()[:200])


def set_occurrences(event, rows, keep_before=None):
    """Make `rows` the event's dates, keeping what the caller didn't mention.

    Replace rather than merge: someone correcting a wrong date expects the
    wrong one to be gone, and `get_or_create` alone would leave it behind.

    Each row is keyed on its start, and only the keys the row actually carries
    are written. That is what lets a refresh restate a page's dates without
    quietly un-cancelling one a moderator cancelled — the refresh rows say
    nothing about `is_cancelled`, so the existing value survives.

    `keep_before` exempts everything starting before it from the replacement.
    A refresh passes "now", because a source page describes what is coming and
    says nothing about what already happened: without this, re-reading a
    weekly class in its ninth month would delete every date it had ever run
    and leave nine months of history looking like it never existed. The paths
    where a human is looking at the whole list on screen pass nothing, since
    there a missing row genuinely means "remove it".
    """
    starts = [row["start"] for row in rows]
    doomed = event.occurrences.exclude(start__in=starts)
    if keep_before is not None:
        doomed = doomed.filter(start__gte=keep_before)
    doomed.delete()

    for row in rows:
        Occurrence.objects.update_or_create(
            event=event,
            start=row["start"],
            defaults={key: value for key, value in row.items() if key != "start"},
        )
