"""The numbers behind the moderator's send log.

One row per run, every figure counted from that run's own deliveries, so
what the log says is what happened rather than what was planned. The few
things that are not on a delivery — who left because of a given week, who
joined between two weeks — are counted alongside in one query each.
"""

from django.db.models import Avg, Count, Q

from .models import DigestDelivery, DigestRun, Subscriber

Status = DigestDelivery.Status


def run_log(limit=26):
    """The latest runs, newest first, each with its figures attached."""
    runs = list(
        DigestRun.objects.defer("text", "html").annotate(
            total=Count("deliveries"),
            sent=Count("deliveries", filter=Q(deliveries__status=Status.SENT)),
            skipped=Count("deliveries", filter=Q(deliveries__status=Status.SKIPPED)),
            failed=Count("deliveries", filter=Q(deliveries__status=Status.FAILED)),
            waiting=Count("deliveries", filter=Q(deliveries__status=Status.PENDING)),
            # Sent emails that led with the reader's own weekend picks.
            personal=Count(
                "deliveries",
                filter=Q(
                    deliveries__status=Status.SENT,
                    deliveries__favorites_count__gt=0,
                ),
            ),
            avg_events=Avg(
                "deliveries__event_count",
                filter=Q(deliveries__status=Status.SENT),
            ),
        )[:limit]
    )
    if not runs:
        return runs

    # Separate from the annotation: joining unsubscribes onto deliveries in
    # the same query would multiply every count above.
    left = dict(
        Subscriber.objects.filter(unsubscribed_via__run__in=runs)
        .order_by()
        .values_list("unsubscribed_via__run")
        .annotate(n=Count("pk"))
    )
    # New readers confirmed between the previous run's start and this one's;
    # for the very first run, everyone who joined before it.
    starts = [r.started_at for r in runs]
    starts.append(
        DigestRun.objects.filter(started_at__lt=runs[-1].started_at)
        .order_by("-started_at")
        .values_list("started_at", flat=True)
        .first()
    )
    for i, run in enumerate(runs):
        run.unsubscribes = left.get(run.pk, 0)
        joined = Subscriber.objects.filter(confirmed_at__lt=run.started_at)
        if starts[i + 1] is not None:
            joined = joined.filter(confirmed_at__gte=starts[i + 1])
        run.joined = joined.count()
    return runs


def joined_since_last_run():
    """Readers confirmed since the latest run started, for "next week" on the page."""
    latest = DigestRun.objects.order_by("-started_at").first()
    rows = Subscriber.objects.active()
    if latest is not None:
        rows = rows.filter(confirmed_at__gte=latest.started_at)
    return rows.count()
