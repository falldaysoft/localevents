"""The listing half of every browse page, apart from the request.

Out of `web.views` because more than the views need it: the weekly email
(`digest.content`) builds its seven days from `_listing` so it collapses
series and splits tiers exactly as the front page does, and a second,
drifting query is the thing this module exists to prevent.
"""

from datetime import timedelta

from django.utils import timezone

from events.models import Event

from .filters import _day_bounds

def _group_by_day(events, now):
    """Consecutive runs of `events` that fall on the same local day.

    A run, not a lookup: `events` arrives ordered by `next_start`, so walking
    it once is enough and the groups come out in date order for free. Feeding
    it a list ordered by anything else would emit the same date twice, which
    is why the featured tier is removed before this is called.

    Each day carries two buckets. `events` are the Listed cards; `quiet` are
    the Quiet listings the front page folds under the day, which a theme may
    render as "+3 more". A day with nothing but quiet listings still gets its
    heading, or the fold would have nothing to hang from.

    The local day matters — an evening event in a timezone behind UTC belongs
    to the day the reader is living in, not the one the column stores.
    """
    today = timezone.localdate(now)
    groups = []
    for event in events:
        day = timezone.localtime(event.next_start).date()
        if not groups or groups[-1]["date"] != day:
            delta = (day - today).days
            groups.append(
                {
                    "date": day,
                    "rel": "Today" if delta == 0 else "Tomorrow" if delta == 1 else "",
                    "events": [],
                    "quiet": [],
                }
            )
        bucket = "quiet" if event.prominence == Event.Prominence.QUIET else "events"
        groups[-1][bucket].append(event)
    for group in groups:
        group["count"] = len(group["events"]) + len(group["quiet"])
    return groups


# Roughly how many cards a browse page shows before it offers "Later". A
# budget rather than a number of days, because a quiet fortnight and a
# festival weekend should both fill about one screenful, and never exact,
# because a page only ever ends between two days.
PAGE_CARDS = 40


def _page_end(listed, quiet, budget, count_quiet):
    """The first day that does not fit on this page, or None if all of it does.

    `listed` and `quiet` are each date-ordered and hold up to budget + 1 rows,
    which is enough to find where the budget runs out. A folded quiet listing
    costs a line inside a fold rather than a card, so it counts only when it
    is shown as one. The page ends *before* the day the budget runs out on,
    never inside it — unless that is the page's first day, which is shown
    whole, or a very busy day could never be reached at all.
    """
    merged = sorted(listed + quiet, key=lambda e: e.next_start)
    first_day = None
    used = 0
    for event in merged:
        if not count_quiet and event.prominence == Event.Prominence.QUIET:
            continue
        day = timezone.localtime(event.next_start).date()
        if first_day is None:
            first_day = day
        used += 1
        if used > budget:
            return day if day != first_day else day + timedelta(days=1)
    return None


# Featured listings are pinned above the day list, and a pile of them pushes
# the first day below the fold. Everything featured within the horizon is
# pinned however many there are, since that is a genuinely busy fortnight;
# further out, only enough to make up the cap.
FEATURED_CAP = 3
FEATURED_HORIZON = timedelta(days=30)


def _pinned_featured(featured, now):
    """The featured listings that sit above the day list, soonest first.

    One that misses the cut is not hidden: it is still in `main_feed`, so it
    joins the date run as an ordinary card on its own day and is pinned once
    its date comes inside the horizon. A moderator's pick is moved, never
    taken away.
    """
    pinned = []
    for event in featured.order_by("next_start"):
        if event.next_start <= now + FEATURED_HORIZON or len(pinned) < FEATURED_CAP:
            pinned.append(event)
    return pinned


def _sponsored(filters, now):
    """Events with a paid promotion running today, as this page would show them.

    Read through the same filter as everything else, so a promoted concert
    does not appear on the family page or a venue it is not at — a sponsor
    buys placement among the listings a reader asked for, not a way out of
    their question. Any tier qualifies: the editor has already decided the
    listing belongs on the site, and the promotion is what moves it up.
    """
    from sponsors.services import promoted_event_ids

    ids = promoted_event_ids(now)
    if not ids:
        return []
    events = list(
        filters.apply(Event.objects.published().filter(pk__in=ids), now).order_by("next_start")
    )
    for event in events:
        event.is_sponsored = True
    return events


def _first_page_floor(today):
    """The earliest day the first page may end before: past this week's Sunday.

    "What's on this week and this weekend" is the question the front page
    exists to answer, so the card budget never cuts into it. From Saturday
    on, "this weekend" is already half gone and the question has become next
    week's, so the floor moves to the following Sunday.
    """
    sunday = today + timedelta(days=6 - today.weekday())
    if today.weekday() >= 5:
        sunday += timedelta(days=7)
    return sunday + timedelta(days=1)


def _listing(filters, now, quiet_horizon_days=None, start=None, paged=True):
    """The listing half of a browse page, for any set of filters.

    Shared by the front page and the venue and category pages, so a landing
    page collapses series and splits tiers exactly as the front page does
    rather than growing a second, drifting query.

    `quiet_horizon_days` is the front page's bargain with the Quiet tier:
    given, Quiet listings further out than that are left off and the rest are
    folded under their day; `None` means a reader asked a narrower question
    (a venue, a category, a search) and Quiet listings are ordinary cards.

    `paged` splits the dated listings into pages of whole days, about
    `PAGE_CARDS` each, and `start` is the day a later page begins on. The
    first page always runs to the end of the week, however busy it is (see
    `_first_page_floor`). It used
    to be a flat cap of 120, which on a busy instance meant everything past
    the fourth week silently was not there. The cut is applied to the
    `next_start` annotation and never by moving `now`, so a weekly listing
    collapses to the same date on every page instead of reappearing on each.
    Pinned featured listings (see `_pinned_featured`) and regular programs
    are not in the date run, so they appear on the first page only. "My events" passes `paged=False`, because
    it decides what has passed by what this returns.

    Finding the boundary costs a second pass: a budget-sized slice of each
    run to find the day it runs out on, then each run again cut at that day.
    """
    tz = timezone.get_current_timezone()
    from_dt = _day_bounds(start, tz)[0] if start else None

    until = None
    if quiet_horizon_days is not None:
        until = now + timedelta(days=quiet_horizon_days)
    # Paid promotions sit with the featured listings, after the editor's own
    # picks and labelled as sponsored, and leave the date run like them.
    sponsored = _sponsored(filters, now)
    sponsored_ids = [e.pk for e in sponsored]
    quiet_all = filters.quiet(now, until=until).exclude(pk__in=sponsored_ids)
    featured = _pinned_featured(
        filters.main_feed(now)
        .filter(prominence=Event.Prominence.FEATURED)
        .exclude(pk__in=sponsored_ids),
        now,
    ) + sponsored
    listed_all = (
        filters.main_feed(now)
        .exclude(pk__in=[e.pk for e in featured])
        .order_by("next_start")
    )
    listed_qs, quiet_qs = listed_all, quiet_all
    if from_dt:
        listed_qs = listed_qs.filter(next_start__gte=from_dt)
        quiet_qs = quiet_qs.filter(next_start__gte=from_dt)

    next_from = None
    if paged:
        next_from = _page_end(
            list(listed_qs[: PAGE_CARDS + 1]),
            list(quiet_qs[: PAGE_CARDS + 1]),
            PAGE_CARDS,
            count_quiet=quiet_horizon_days is None,
        )
        if next_from and start is None:
            next_from = max(next_from, _first_page_floor(timezone.localdate(now)))
        if next_from:
            to_dt = _day_bounds(next_from, tz)[0]
            listed_qs = listed_qs.filter(next_start__lt=to_dt)
            quiet_qs = quiet_qs.filter(next_start__lt=to_dt)
            # A first day shown whole can be the last day there is, and a
            # "Later" that leads to an empty page is worse than none.
            if not (
                listed_all.filter(next_start__gte=to_dt).exists()
                or quiet_all.filter(next_start__gte=to_dt).exists()
            ):
                next_from = None

    listed = list(listed_qs)
    quiet = list(quiet_qs)
    first_page = start is None
    if not first_page:
        featured = []
    programs = (
        list(filters.programs(now).exclude(pk__in=sponsored_ids)) if first_page else []
    )

    # Folded or not, a quiet listing sits under its own day. Merged by date
    # with the listed run so `_group_by_day` sees one ordered walk; `sorted`
    # is stable, so within a day the listed cards keep their order and the
    # quiet ones follow. Featured listings are kept out of the walk so a
    # theme can group by date without one appearing under a day weeks away.
    by_day = sorted(listed + quiet, key=lambda e: e.next_start)

    return {
        "events": featured + listed,
        "featured": featured,
        "days": _group_by_day(by_day, now),
        "quiet": quiet,
        "quiet_folded": quiet_horizon_days is not None,
        "quiet_horizon_days": quiet_horizon_days,
        "programs": programs,
        "program_count": len(programs),
        # The whole answer, not this page of it: "212 events" is what tells a
        # reader there is a "Later" worth pressing.
        "result_count": filters.main_feed(now).count() + quiet_all.count(),
        "page_from": start,
        "next_from": next_from,
    }
