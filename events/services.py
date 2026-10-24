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


def set_venue(current, name, address, city):
    """The venue an event should point at after someone edited its place fields.

    `venue_for` is deliberately conservative: it will fill a blank address but
    never overwrite one, because a submitter typing a half-remembered address
    must not be able to rewrite a venue every other listing shares. That is the
    right rule for a stranger and the wrong one for the person whose job is
    fixing the record — a moderator who has just been told the address is wrong
    needs the correction to take.

    So the two edits are separated by what changed:

    - The **name** changed, or the town did: that is choosing a *different*
      venue, so the event repoints and the old record is left exactly as it
      was. A rename here never renames a hall out from under twelve other
      listings, which is the failure the old dropdown existed to prevent.
    - Only the **address** changed: that is correcting the venue everyone
      shares, so it is written through — and the coordinates it was geocoded
      to are now for the wrong place, so it goes back in the geocode queue.
      Coordinates set by hand survive; `geocode_venue` returns early on
      `MANUAL` rather than overwriting someone's deliberate pin.
    """
    name, address, city = name.strip(), address.strip(), city.strip()

    if not name:
        return None

    same_venue = (
        current is not None
        and current.name.casefold() == name.casefold()
        and current.city.casefold() == city.casefold()
    )
    if same_venue:
        if address != current.address:
            current.address = address[:300]
            current.geocode_status = Venue.GeocodeStatus.PENDING
            current.save(update_fields=["address", "geocode_status"])
            geocode_venue.enqueue(current.pk)
        return current

    return venue_for(name, address, city)


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


# ---------------------------------------------------------------------------
# Interest
# ---------------------------------------------------------------------------


def toggle_interest(event, *, user=None, fingerprint=None):
    """One person's "Interested" mark on one event — on or off.

    The same signal whoever gives it: a reader on the event page, an anonymous
    visitor identified by their cookie, or a moderator from the events screen.
    It buys the same thing in every case — a tie-break within a tier and a
    shot at the Rising queue once `MIN_INTEREST_TO_RISE` others agree — and it
    is not an audit event, because the row already names who gave it.

    Exactly one of `user` and `fingerprint` identifies the person. Returns
    whether they are now interested.
    """
    from django.db import IntegrityError, transaction

    from events.models import Interest

    if (user is None) == (fingerprint is None):
        raise ValueError("Identify the person by user or by fingerprint, not both.")

    if user is not None:
        rows = Interest.objects.filter(event=event, user=user)
        fields = {"user": user}
    else:
        rows = Interest.objects.filter(
            event=event, user__isnull=True, fingerprint=fingerprint
        )
        fields = {"fingerprint": fingerprint}

    existing = rows.first()
    if existing is not None:
        existing.delete()
        return False
    try:
        with transaction.atomic():
            Interest.objects.create(event=event, **fields)
    except IntegrityError:
        # Two clicks landed together and the other one won. The person is
        # interested, which is what they asked for either way.
        pass
    return True


def merge_anonymous_interest(user, fingerprint):
    """Hand a device's anonymous marks to the account that just signed in.

    Re-pointed with an UPDATE rather than through `save()`, which would count
    each mark a second time. Where the account already holds a mark on an
    event the device also marked, the anonymous twin is deleted through the
    model so the count comes down by the one it is losing.
    """
    from events.models import Interest

    if not fingerprint:
        return
    anonymous = Interest.objects.filter(user__isnull=True, fingerprint=fingerprint)
    already = set(
        Interest.objects.filter(user=user).values_list("event_id", flat=True)
    )
    for twin in anonymous.filter(event_id__in=already):
        twin.delete()
    anonymous.update(user=user, fingerprint="")
