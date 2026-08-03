"""The moderators' side of the site.

Not Django admin. The admin is a table editor; this is a decision surface —
one submission at a time, with everything needed to judge it on one screen and
three buttons at the bottom. A volunteer working through a Sunday evening
backlog should never have to open a second tab to find out what a page said.
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from core.models import SiteConfig
from submissions.models import ModerationAction, Submission

from . import services
from .forms import ApproveForm, RejectForm, RequestInfoForm
from .permissions import moderator_required


def _chrome(request, **extra):
    """Context every moderation page needs."""
    return {
        "queue_counts": services.queue_counts(request.user),
        "site_config": SiteConfig.load(),
        **extra,
    }


@moderator_required
def queue(request):
    view = request.GET.get("view", "review")
    if view not in services.QUEUE_VIEWS:
        view = "review"

    return render(
        request,
        "moderation/queue.html",
        _chrome(
            request,
            view=view,
            view_label=services.QUEUE_VIEWS[view],
            views=services.QUEUE_VIEWS,
            submissions=services.queue_for(view, request.user)[:200],
        ),
    )


@moderator_required
def submission_detail(request, pk):
    """Everything needed to decide, on one screen."""
    submission = get_object_or_404(
        Submission.objects.select_related(
            "submitted_by", "assigned_to", "decided_by", "event", "event__venue",
            "event__organizer",
        ).prefetch_related(
            "messages__author", "actions__actor", "enrichment_runs",
            "event__categories",
        ),
        pk=pk,
    )
    event = submission.event

    return render(
        request,
        "moderation/submission.html",
        _chrome(
            request,
            submission=submission,
            event=event,
            occurrences=(
                list(event.occurrences.order_by("start")[:30]) if event else []
            ),
            # Not a blocker — plenty of communities care about something just
            # over the county line — but a moderator should be told.
            out_of_region=bool(event and event.needs_region_review()),
            approve_form=ApproveForm(
                initial={
                    "prominence": event.prominence if event else None,
                    "listing_type": event.listing_type if event else None,
                    "categories": (
                        list(event.categories.values_list("pk", flat=True))
                        if event
                        else []
                    ),
                }
            ),
            reject_form=RejectForm(),
            info_form=RequestInfoForm(),
        ),
    )


@moderator_required
@require_POST
def assign(request, pk):
    submission = get_object_or_404(Submission, pk=pk)
    if submission.assigned_to_id == request.user.pk:
        services.unassign(submission, request.user)
        messages.success(request, "Handed back to the queue.")
    else:
        services.assign(submission, request.user)
        messages.success(request, "Assigned to you.")
    return redirect("mod_submission", pk=pk)


@moderator_required
@require_POST
def approve(request, pk):
    submission = get_object_or_404(
        Submission.objects.select_related("event"), pk=pk
    )
    form = ApproveForm(request.POST)

    if not form.is_valid():
        messages.error(request, "Choose where this sits before publishing.")
        return redirect("mod_submission", pk=pk)

    try:
        event = services.approve(
            submission,
            request.user,
            prominence=form.cleaned_data["prominence"],
            listing_type=form.cleaned_data["listing_type"],
            categories=form.cleaned_data["categories"],
            note=form.cleaned_data["note"],
        )
    except services.NothingToApprove as exc:
        messages.error(request, str(exc))
        return redirect("mod_submission", pk=pk)

    messages.success(request, f"“{event.title}” is live.")
    return redirect("mod_queue")


@moderator_required
@require_POST
def reject(request, pk):
    submission = get_object_or_404(
        Submission.objects.select_related("event"), pk=pk
    )
    form = RejectForm(request.POST)

    if not form.is_valid():
        messages.error(request, "A rejection needs a reason.")
        return redirect("mod_submission", pk=pk)

    services.reject(submission, request.user, reason=form.cleaned_data["reason"])
    messages.success(request, "Declined, and the submitter has been told why.")
    return redirect("mod_queue")


@moderator_required
@require_POST
def request_info(request, pk):
    submission = get_object_or_404(Submission, pk=pk)
    form = RequestInfoForm(request.POST)

    if not form.is_valid():
        messages.error(request, "Write the question you want to ask.")
        return redirect("mod_submission", pk=pk)

    services.request_info(submission, request.user, body=form.cleaned_data["body"])
    messages.success(request, "Asked. It's back with the submitter now.")
    return redirect("mod_queue")


@moderator_required
def rising(request):
    """Promotion nominations from the crowd.

    Read-only until a moderator clicks. Interest never moves an event between
    tiers on its own — that bound is what makes anonymous interest with weak
    deduplication a safe signal to collect at all.
    """
    return render(
        request,
        "moderation/rising.html",
        _chrome(request, events=services.rising_events()),
    )


@moderator_required
@require_POST
def promote(request, pk):
    from events.models import Event

    event = get_object_or_404(Event, pk=pk, status=Event.Status.PUBLISHED)
    before = event.get_prominence_display()
    services.promote(event, request.user)

    if event.get_prominence_display() == before:
        messages.info(request, "Already as prominent as it gets.")
    else:
        messages.success(
            request, f"“{event.title}” is now {event.get_prominence_display()}."
        )
    return redirect("mod_rising")


@moderator_required
def audit(request):
    """Who did what.

    Volunteer moderators come and go. When a decision is questioned months
    later the answer cannot depend on anyone's memory.
    """
    actions = ModerationAction.objects.select_related(
        "actor", "submission", "event"
    )[:300]
    return render(request, "moderation/audit.html", _chrome(request, actions=actions))
