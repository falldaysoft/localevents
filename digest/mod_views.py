"""The weekly email, from a moderator's side.

Moderators do not send it — housekeeping does, on the schedule in SiteConfig.
What they get here is the master switch, a log of what each week's run did,
a test send of what the email would say to them right now, and the
subscriber list, where someone who asked by phone or in person can be added
or taken off.
"""

from django.contrib import messages
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.models import SiteConfig
from events import interest
from moderation.permissions import moderator_required

from . import services, stats
from .forms import ModeratorSubscribeForm, NoteForm
from .models import DigestDelivery, DigestRun, Subscriber
from .sending import send_test

# A test goes through the real relay, so it is capped: a moderator checking a
# template change sends a few, not a few hundred.
TESTS_PER_HOUR = 10


def _chrome(request, **extra):
    from moderation import services

    return {
        "queue_counts": services.queue_counts(request.user),
        "site_config": SiteConfig.load(),
        **extra,
    }


@moderator_required
def overview(request):
    config = SiteConfig.load()
    return render(
        request,
        "digest/moderate.html",
        _chrome(
            request,
            config=config,
            weekday=config.get_digest_weekday_display(),
            active_count=Subscriber.objects.active().count(),
            pending_count=Subscriber.objects.pending().count(),
            joined_count=stats.joined_since_last_run(),
            runs=stats.run_log(),
        ),
    )


@moderator_required
@require_POST
def switch(request):
    on = request.POST.get("on") == "1"
    if services.set_enabled(request.user, on):
        if on:
            config = SiteConfig.load()
            messages.success(
                request,
                f"The weekly email is on. The next one goes out on "
                f"{config.get_digest_weekday_display()} from "
                f"{config.digest_hour:02d}:00, and the signup form is open.",
            )
        else:
            messages.success(
                request,
                "The weekly email is off: nothing more will be sent, and the "
                "signup form is hidden.",
            )
    return redirect("mod_digest")


@moderator_required
@require_POST
def test_send(request):
    """Send the moderator the email they would get if it went out now."""
    if not request.user.email:
        messages.error(request, "Your account has no email address to send to.")
        return redirect("mod_digest")

    key = f"digest-tests:{request.user.pk}"
    cache.add(key, 0, timeout=60 * 60)
    if cache.incr(key) > TESTS_PER_HOUR:
        messages.error(request, "That's enough test emails for this hour.")
        return redirect("mod_digest")

    try:
        digest = send_test(request.user, fingerprint=interest.fingerprint(request) or "")
    except Exception as exc:
        messages.error(request, f"The test email could not be sent: {exc}")
    else:
        messages.success(
            request,
            f"Sent to {request.user.email}: “{digest.subject}” "
            f"({digest.event_count} events, {digest.favorites_count} of yours this weekend).",
        )
    return redirect("mod_digest")


DELIVERY_FILTERS = {
    "sent": DigestDelivery.Status.SENT,
    "skipped": DigestDelivery.Status.SKIPPED,
    "failed": DigestDelivery.Status.FAILED,
    "waiting": DigestDelivery.Status.PENDING,
}


@moderator_required
def run_detail(request, pk):
    """Every recipient of one week's email, and what happened to each."""
    run = get_object_or_404(DigestRun, pk=pk)
    deliveries = run.deliveries.all()
    shown = request.GET.get("status", "")
    if shown in DELIVERY_FILTERS:
        deliveries = deliveries.filter(status=DELIVERY_FILTERS[shown])
    else:
        shown = ""
    q = request.GET.get("q", "").strip()
    if q:
        deliveries = deliveries.filter(email__icontains=q)
    left = set(
        Subscriber.objects.filter(unsubscribed_via__run=run).values_list(
            "unsubscribed_via", flat=True
        )
    )
    page = Paginator(deliveries, 100).get_page(request.GET.get("page"))
    counts = dict(
        run.deliveries.order_by().values_list("status").annotate(n=Count("pk"))
    )
    return render(
        request,
        "digest/moderate_run.html",
        _chrome(
            request,
            run=run,
            page=page,
            shown=shown,
            q=q,
            left=left,
            counts={key: counts.get(value, 0) for key, value in DELIVERY_FILTERS.items()},
            total=sum(counts.values()),
        ),
    )


SUBSCRIBER_FILTERS = {
    "active": lambda rows: rows.active(),
    "pending": lambda rows: rows.pending(),
    "unsubscribed": lambda rows: rows.filter(unsubscribed_at__isnull=False),
}


@moderator_required
def subscribers(request):
    """Find a subscriber; add someone who asked outside the site."""
    form = ModeratorSubscribeForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        subscriber, changed = services.moderator_subscribe(
            request.user, form.cleaned_data["email"], form.cleaned_data["note"]
        )
        if changed:
            messages.success(request, f"{subscriber.email} will get the weekly email.")
        else:
            messages.info(request, f"{subscriber.email} already gets the weekly email.")
        return redirect("mod_digest_subscriber", subscriber.pk)

    rows = Subscriber.objects.select_related("user")
    shown = request.GET.get("status", "active")
    if shown in SUBSCRIBER_FILTERS:
        rows = SUBSCRIBER_FILTERS[shown](rows)
    else:
        shown = "all"
    q = request.GET.get("q", "").strip()
    if q:
        rows = rows.filter(email__icontains=q)
    page = Paginator(rows, 50).get_page(request.GET.get("page"))
    return render(
        request,
        "digest/moderate_subscribers.html",
        _chrome(request, page=page, shown=shown, q=q, form=form),
        status=400 if request.method == "POST" else 200,
    )


@moderator_required
def subscriber_detail(request, pk):
    """One address: its consent record, what it was sent, and a switch."""
    subscriber = get_object_or_404(Subscriber.objects.select_related("user"), pk=pk)
    form = NoteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        note = form.cleaned_data["note"]
        if request.POST.get("action") == "subscribe":
            services.moderator_subscribe(request.user, subscriber.email, note)
            messages.success(request, f"{subscriber.email} will get the weekly email.")
        else:
            services.moderator_unsubscribe(request.user, subscriber, note)
            messages.success(request, f"{subscriber.email} won't get the weekly email any more.")
        return redirect("mod_digest_subscriber", subscriber.pk)

    deliveries = subscriber.deliveries.select_related("run").order_by("-run__send_date")[:52]
    return render(
        request,
        "digest/moderate_subscriber.html",
        _chrome(request, subscriber=subscriber, deliveries=deliveries, form=form),
        status=400 if request.method == "POST" else 200,
    )
