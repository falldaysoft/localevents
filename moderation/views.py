"""The moderators' side of the site.

Not Django admin. The admin is a table editor; this is a decision surface —
one submission at a time, with everything needed to judge it on one screen and
three buttons at the bottom. A volunteer working through a Sunday evening
backlog should never have to open a second tab to find out what a page said.
"""

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.models import SiteConfig
from submissions.models import ModerationAction, Submission

from . import services
from .forms import (
    ApproveForm,
    EventEditForm,
    ModeratorOccurrenceFormSet,
    RefreshApplyForm,
    RejectForm,
    RequestInfoForm,
)
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


@moderator_required
def event_edit(request, pk):
    """Fix a listing that is already live.

    Reached from the event page itself rather than from the queue, because
    that is where the problem gets noticed — someone reads the page, spots the
    wrong time, and the way to fix it should be on the screen showing the
    mistake.

    Any status is editable, not just published ones: an event pulled down for
    a correction has to be reachable to put back up.
    """
    from events.models import Event

    event = get_object_or_404(
        Event.objects.select_related("venue", "organizer").prefetch_related(
            "categories"
        ),
        pk=pk,
    )

    if request.method == "POST":
        form = EventEditForm(request.POST, instance=event)
        if form.is_valid():
            changed = form.changed_data
            event = form.save()

            # A save that changed nothing is not worth a line in the log; the
            # audit trail is read by someone looking for what happened.
            if changed:
                ModerationAction.record(
                    request.user,
                    "event_edited",
                    event=event,
                    detail=", ".join(changed),
                )
                messages.success(request, f"“{event.title}” updated.")
            else:
                messages.info(request, "Nothing changed.")

            # An unpublished event has no public page to go back to.
            if event.status == Event.Status.PUBLISHED:
                return redirect("event_detail", slug=event.slug)
            return redirect("mod_event_edit", pk=event.pk)
    else:
        form = EventEditForm(instance=event)

    return render(
        request,
        "moderation/event_edit.html",
        _chrome(request, event=event, form=form),
    )


@moderator_required
def event_dates(request, pk):
    """When a listing happens, on its own screen.

    Split out from the edit form deliberately. Everything else there is text a
    mistake in is embarrassing; a mistake here sends someone to a locked hall,
    and burying the dates among twenty other fields is how one gets changed by
    accident while somebody fixes a typo.

    Until this existed the only way to add a second date to a published event
    was the Django admin — which means a staff account, which is a far larger
    grant than "may correct a listing". Events entered before an occurrence
    could carry its own end time are the reason it was needed: they hold one
    date where the source page always listed six.
    """
    from events.models import Event
    from events.services import set_occurrences
    from submissions.forms import formset_with_more_rows

    event = get_object_or_404(Event, pk=pk)

    if request.method == "POST":
        dates = ModeratorOccurrenceFormSet(request.POST, prefix="dates")

        if "add_dates" in request.POST:
            dates = formset_with_more_rows(
                request.POST, ModeratorOccurrenceFormSet
            )
        elif dates.is_valid():
            rows = dates.dates()
            set_occurrences(event, rows)
            ModerationAction.record(
                request.user,
                "dates_edited",
                event=event,
                detail=f"{len(rows)} date{'' if len(rows) == 1 else 's'} — {event.title}",
            )
            messages.success(request, f"Dates updated for “{event.title}”.")
            return redirect("mod_event_edit", pk=event.pk)
    else:
        dates = ModeratorOccurrenceFormSet(
            prefix="dates", initial=_date_rows(event)
        )

    return render(
        request,
        "moderation/event_dates.html",
        _chrome(request, event=event, dates=dates),
    )


def _date_rows(event):
    """An event's occurrences as formset initial data.

    Localised on the way out because a `datetime-local` input has no timezone
    of its own: handing it a UTC value renders the right instant under the
    wrong clock, and a moderator would "fix" a 7pm concert that was never
    wrong.
    """
    from django.utils import timezone as tz

    return [
        {
            "start": tz.localtime(o.start),
            "end": tz.localtime(o.end) if o.end else None,
            "note": o.note,
            "is_cancelled": o.is_cancelled,
        }
        for o in event.occurrences.order_by("start")
    ]


# ---------------------------------------------------------------------------
# Refreshing a listing from its source page
# ---------------------------------------------------------------------------


@moderator_required
def event_refresh(request, pk):
    """Read the source page again and offer a moderator what changed.

    A GET shows whatever the last run produced; a POST either starts a run,
    takes some of its changes, or throws it away. Nothing is ever written to
    the event without a tick against it — see `events.refresh` for why that is
    not negotiable.
    """
    from events.models import Event
    from events.refresh import (
        apply_refresh,
        changes_for,
        discard_refresh,
        request_refresh,
    )

    event = get_object_or_404(
        Event.objects.select_related("venue", "organizer"), pk=pk
    )
    refresh = event.refreshes.first()

    # A worker that died mid-read never releases its claim, so a page that
    # only ever polls would spin forever.
    if refresh is not None and refresh.is_stranded:
        refresh.give_up()

    if request.method == "POST":
        if "start" in request.POST:
            if not event.source_url:
                messages.error(
                    request, "This listing has no source page to read."
                )
                return redirect("mod_event_refresh", pk=event.pk)
            request_refresh(event, request.user)
            return redirect("mod_event_refresh", pk=event.pk)

        if refresh is None or refresh.status != refresh.Status.READY:
            messages.error(request, "That proposal is no longer current.")
            return redirect("mod_event_refresh", pk=event.pk)

        if "discard" in request.POST:
            discard_refresh(refresh, request.user)
            messages.info(request, "Discarded. The listing is unchanged.")
            return redirect("mod_event_edit", pk=event.pk)

        form = RefreshApplyForm(changes_for(refresh), request.POST)
        if form.is_valid():
            taken = apply_refresh(refresh, form.chosen(), request.user)
            if taken:
                ModerationAction.record(
                    request.user,
                    "refreshed_from_source",
                    event=event,
                    detail=f"{', '.join(taken)} — {event.title}",
                )
                messages.success(
                    request, f"Updated {', '.join(taken).lower()} from the page."
                )
            else:
                messages.info(request, "Nothing taken. The listing is unchanged.")
            return redirect("mod_event_edit", pk=event.pk)
    else:
        form = None

    changes = (
        changes_for(refresh)
        if refresh is not None and refresh.status == refresh.Status.READY
        else []
    )

    return render(
        request,
        "moderation/event_refresh.html",
        _chrome(
            request,
            event=event,
            refresh=refresh,
            changes=changes,
            form=form or RefreshApplyForm(changes),
        ),
    )


@moderator_required
def event_refresh_status(request, pk):
    """HTMX poll target while a page is being read."""
    from events.models import Event

    event = get_object_or_404(Event, pk=pk)
    refresh = event.refreshes.first()

    if refresh is not None and refresh.is_stranded:
        refresh.give_up()

    response = render(
        request,
        "moderation/event_refresh.html#status",
        {"event": event, "refresh": refresh},
    )
    if refresh is None or not refresh.is_running:
        # Stop polling and show the result.
        response["HX-Redirect"] = reverse("mod_event_refresh", args=[event.pk])
    return response


@moderator_required
def refresh_queue(request):
    """Every re-read waiting on a decision.

    Without this a bulk refresh is invisible: the admin can queue fifty and
    each result would sit on a listing nobody has a reason to open.
    """
    from events.models import EventRefresh

    refreshes = (
        EventRefresh.objects.awaiting_review()
        .select_related("event", "event__venue", "requested_by")
        .order_by("-created_at")[:100]
    )
    return render(
        request,
        "moderation/refreshes.html",
        _chrome(request, refreshes=refreshes),
    )
