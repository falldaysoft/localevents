"""The weekly email, from a moderator's side.

Moderators do not send it — housekeeping does, on the schedule in SiteConfig.
What they get here is a view of that schedule and a test send of what the
email would say to them right now. The send log (localevents.14.4) joins it.
"""

from django.contrib import messages
from django.core.cache import cache
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from core.models import SiteConfig
from events import interest
from moderation.permissions import moderator_required

from .models import Subscriber
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
        ),
    )


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
