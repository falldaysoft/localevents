"""Is this listing already on the site?

Two people submit the same fair from two different pages — the organiser's
own and a council calendar — and each submission looks fine on its own. The
moderator sees one at a time, so nothing put the two side by side and both
were published. This finds the likely twin and shows it; it never blocks.
A human decides, because the near-misses are real: two open mics on the same
night at different halls are two events.

A match is either the same source page, or a similar title on overlapping
dates. Titles are compared loosely — accents, punctuation, case and small
words dropped — because the two copies are written by different people and
one says "Le Petit Renaissance Faire" where the other says "Le Petit
Renaissance Faire at Le Petit Marché". Venue is deliberately not required:
the duplicate that prompted this had no venue at all.
"""

import re
import unicodedata
from datetime import timedelta
from difflib import SequenceMatcher
from urllib.parse import urlsplit

from django.db.models import Q

from .models import Event, occurrence_overlaps

# An event entered with the wrong start time, or in a different timezone
# convention, still overlaps its twin within a day.
DATE_SLACK = timedelta(days=1)

# Below this the titles are merely about the same kind of thing.
TITLE_RATIO = 0.8
TOKEN_OVERLAP = 0.6

STOP_WORDS = {
    "a", "an", "and", "at", "de", "du", "for", "in", "la", "le", "les", "of",
    "on", "the", "to", "with",
}

# Pending as well as published: two copies waiting in the queue at once is
# exactly how both get approved.
LIVE_STATUSES = [Event.Status.PUBLISHED, Event.Status.PENDING]


def _tokens(title):
    text = unicodedata.normalize("NFKD", title or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    words = re.findall(r"[a-z0-9]+", text)
    return [w for w in words if w not in STOP_WORDS] or words


def titles_match(a, b):
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return False
    if SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio() >= TITLE_RATIO:
        return True
    # One title containing the other plus a venue or a year.
    sa, sb = set(ta), set(tb)
    return len(sa & sb) / min(len(sa), len(sb)) >= TOKEN_OVERLAP and len(sa & sb) >= 2


def _normal_url(url):
    if not url:
        return ""
    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    return f"{host}{parts.path.rstrip('/')}"


def possible_duplicates(event, limit=5):
    """Other live events that look like this one, with the reason for each."""
    spans = [(o.start, o.end) for o in event.occurrences.all()[:60]]
    return find_duplicates(
        event.title, event.source_url, spans, exclude_pk=event.pk, limit=limit
    )


def find_duplicates(title, source_url, spans, exclude_pk=None, limit=5):
    """The same check for a listing that need not exist yet.

    Takes the parts rather than an Event so the API can answer "is this
    already on the site?" before anything is written — saving a draft to ask
    would create its venue and queue a geocode for a listing that may never be
    submitted. `spans` is a list of (start, end-or-None) pairs.
    """
    windows = Q()
    for start, end in spans:
        windows |= occurrence_overlaps(
            start - DATE_SLACK, (end or start) + DATE_SLACK, prefix="occurrences"
        )

    candidates = Event.objects.filter(status__in=LIVE_STATUSES)
    if exclude_pk:
        candidates = candidates.exclude(pk=exclude_pk)

    found = {}
    if spans:
        for other in candidates.filter(windows).distinct().select_related("venue")[:200]:
            if titles_match(title, other.title):
                found[other.pk] = (other, "Similar title on the same dates")

    source = _normal_url(source_url)
    if source:
        host = source.split("/", 1)[0]
        for other in candidates.filter(source_url__icontains=host).select_related("venue")[:200]:
            if other.pk not in found and _normal_url(other.source_url) == source:
                found[other.pk] = (other, "Same source page")

    return [
        {"event": other, "reason": reason}
        for other, reason in sorted(found.values(), key=lambda pair: pair[0].pk)
    ][:limit]
