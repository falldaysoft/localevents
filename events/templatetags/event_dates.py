"""Formatting a date that might be a span.

Every listing surface — the card, the detail page, the moderation queue — has
to answer the same awkward question: an occurrence has a start and *maybe* an
end, and the end may or may not be on the same day. Three shapes, and each
template getting it slightly wrong is how "Sat 6 Sep, 7:00 a.m. – 2:00 p.m."
ends up rendered as "Sat 6 Sep, 7:00 a.m." on one page and, worse, as
"Fri 5 Sep, 6:00 p.m. – 5:00 p.m." on another when the thing actually runs
until Sunday.

So the decision lives here once, and the templates ask for a string.
"""

from django import template
from django.utils import timezone
from django.utils.formats import date_format

register = template.Library()

# Deliberately not settings — these are typography, not configuration, and no
# instance should be choosing its own date format for the sake of it.
DAY = "D j M"
DAY_WITH_YEAR = "D j M Y"
TIME = "g:i a"


def _local(value):
    return timezone.localtime(value) if value is not None else None


def _fmt(value, pattern):
    return date_format(value, pattern)


@register.simple_tag
def occurrence_when(start, end=None, with_year=False):
    """A start and optional end as one readable phrase.

    Three cases, in the order they come up:

    - no end        → "Sat 6 Sep, 7:00 a.m."
    - ends same day → "Sat 6 Sep, 7:00 a.m. – 2:00 p.m."
    - ends later    → "Fri 5 Sep, 6:00 p.m. – Sun 7 Sep, 5:00 p.m."

    Only the last one is genuinely new, and it is the whole point: a festival
    that runs a weekend has to say so on the card, or a reader checking on
    Saturday has no way to tell it is still on.
    """
    start = _local(start)
    if start is None:
        return ""

    day = DAY_WITH_YEAR if with_year else DAY
    opening = f"{_fmt(start, day)}, {_fmt(start, TIME)}"

    end = _local(end)
    if end is None or end <= start:
        return opening

    if end.date() == start.date():
        return f"{opening} – {_fmt(end, TIME)}"

    return f"{opening} – {_fmt(end, day)}, {_fmt(end, TIME)}"


@register.simple_tag
def occurrence_days(start, end=None, with_year=False):
    """Just the days, for when the hours would be noise.

    "Fri 5 – Sun 7 Sep" is what a multi-day listing wants in a heading; the
    times belong in the body.
    """
    start = _local(start)
    if start is None:
        return ""

    day = DAY_WITH_YEAR if with_year else DAY
    end = _local(end)
    if end is None or end.date() == start.date():
        return _fmt(start, day)
    return f"{_fmt(start, DAY)} – {_fmt(end, day)}"


@register.filter
def is_under_way(start, end=None):
    """Is this happening right now?

    Templates get an annotated `next_start`/`next_end` pair rather than an
    Occurrence, so this cannot live on the model alone.
    """
    if start is None:
        return False
    now = timezone.now()
    return start <= now <= (end or start)
