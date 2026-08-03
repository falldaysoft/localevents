from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from core.models import SiteConfig
from events.models import Category

from .forms import EventDraftForm, StartSubmissionForm
from .models import Submission, SubmissionMessage, SubmissionQuota
from .services import save_event_from_draft
from .sources import advice_for
from .tasks import enrich_submission


@login_required
def start(request):
    """Paste a link — or say you haven't got one."""
    quota = SubmissionQuota.for_user(request.user)

    if not quota.may_submit():
        return render(
            request,
            "submissions/quota_reached.html",
            {"quota": quota, "site_config": SiteConfig.load()},
        )

    if request.method == "POST":
        form = StartSubmissionForm(request.POST)
        if form.is_valid():
            url = form.cleaned_data.get("source_url", "")
            manual = form.cleaned_data.get("manual") or not url

            submission = Submission.objects.create(
                submitted_by=request.user,
                source_url=url,
                source_advice=advice_for(url),
                status=(
                    Submission.Status.AWAITING_SUBMITTER
                    if manual
                    else Submission.Status.NEW
                ),
            )

            if manual:
                submission.enrichment_message = (
                    "Tell us about the event and we'll pass it to a moderator."
                )
                submission.save(update_fields=["enrichment_message", "updated_at"])
            elif not quota.may_enrich():
                # Reading pages costs money; submitting does not. Someone who
                # burns through page reads without submitting anything can
                # still submit by hand.
                submission.status = Submission.Status.AWAITING_SUBMITTER
                submission.enrichment_failed = True
                submission.enrichment_message = (
                    "You've used today's automatic page reads. You can still "
                    "enter the details yourself."
                )
                submission.save()
            else:
                enrich_submission.enqueue(submission.pk)

            return redirect("submission_detail", pk=submission.pk)
    else:
        form = StartSubmissionForm()

    return render(
        request,
        "submissions/start.html",
        {"form": form, "site_config": SiteConfig.load()},
    )


@login_required
def submission_detail(request, pk):
    """Wait for the read, then confirm the details."""
    submission = get_object_or_404(
        Submission, pk=pk, submitted_by=request.user
    )

    # A worker that died mid-task never releases its claim, so check before
    # showing a spinner that would otherwise never resolve.
    if submission.is_stranded:
        submission.recover_from_stranding()

    if submission.status in {Submission.Status.NEW, Submission.Status.ENRICHING}:
        return render(
            request,
            "submissions/working.html",
            {"submission": submission, "site_config": SiteConfig.load()},
        )

    if not submission.is_editable_by_submitter:
        return render(
            request,
            "submissions/status.html",
            {"submission": submission, "site_config": SiteConfig.load()},
        )

    if request.method == "POST":
        form = EventDraftForm(request.POST)
        if form.is_valid():
            reply = form.cleaned_data.get("reply", "").strip()
            if reply:
                SubmissionMessage.objects.create(
                    submission=submission, author=request.user, body=reply
                )
            event = save_event_from_draft(submission, form.cleaned_data)
            messages.success(
                request,
                f"Thanks — “{event.title}” is with the moderators now. "
                "We'll email you when it's been looked at.",
            )
            return redirect("my_submissions")
    else:
        # After a moderator asks a question the event already exists, and it —
        # not the original extraction — is what the submitter has been asked
        # about. Prefilling from the stale draft would silently undo their own
        # earlier corrections.
        initial = (
            _initial_from_event(submission.event)
            if submission.event_id
            else _initial_from_draft(submission.draft)
        )
        form = EventDraftForm(initial=initial)

    return render(
        request,
        "submissions/confirm.html",
        {
            "submission": submission,
            "form": form,
            "categories": Category.objects.filter(is_active=True),
            "site_config": SiteConfig.load(),
        },
    )


def _initial_from_event(event):
    """Map an existing event back onto the confirmation form."""
    occurrences = list(event.occurrences.order_by("start"))
    first = occurrences[0] if occurrences else None

    return {
        "title": event.title,
        "summary": event.summary,
        "description": event.description,
        "venue_name": event.venue.name if event.venue else "",
        "venue_address": event.venue.address if event.venue else "",
        "venue_city": event.venue.city if event.venue else "",
        "organizer_name": event.organizer.name if event.organizer else "",
        "starts_at": first.start if first else None,
        "ends_at": first.end if first else None,
        "is_series": event.is_series,
        "additional_dates": "\n".join(
            timezone.localtime(o.start).strftime("%Y-%m-%d %H:%M")
            for o in occurrences[1:]
        ),
        "is_free": event.is_free,
        "price_note": event.price_note,
        "is_family_friendly": event.is_family_friendly,
        "accessibility_notes": event.accessibility_notes,
        "categories": list(event.categories.values_list("pk", flat=True)),
        "source_url": event.source_url,
        "ticket_url": event.ticket_url,
    }


def _initial_from_draft(draft):
    """Map an extracted draft onto the confirmation form.

    The draft is deliberately loose — a partial extraction is still useful — so
    every field here tolerates being absent.
    """
    if not draft:
        return {}

    occurrences = draft.get("occurrences") or []
    first = occurrences[0] if occurrences else {}

    slugs = draft.get("category_slugs") or []
    category_ids = list(
        Category.objects.filter(slug__in=slugs, is_active=True).values_list(
            "id", flat=True
        )
    )

    extra = "\n".join(
        occurrence["start"].replace("T", " ")[:16]
        for occurrence in occurrences[1:]
        if occurrence.get("start")
    )

    return {
        "title": draft.get("title", ""),
        "summary": draft.get("summary", ""),
        "description": draft.get("description", ""),
        "venue_name": draft.get("venue_name", ""),
        "venue_address": draft.get("venue_address", ""),
        "venue_city": draft.get("venue_city", ""),
        "organizer_name": draft.get("organizer_name", ""),
        "starts_at": (first.get("start") or "").replace("T", " ")[:16] or None,
        "ends_at": (first.get("end") or "").replace("T", " ")[:16] or None,
        "is_series": draft.get("is_series", False),
        "additional_dates": extra,
        "is_free": draft.get("is_free", False),
        "price_note": draft.get("price_note", ""),
        "is_family_friendly": draft.get("is_family_friendly", False),
        "accessibility_notes": draft.get("accessibility_notes", ""),
        "categories": category_ids,
        "ticket_url": draft.get("ticket_url", ""),
    }


@login_required
def submission_status_fragment(request, pk):
    """HTMX poll target while the page is being read."""
    submission = get_object_or_404(Submission, pk=pk, submitted_by=request.user)

    if submission.is_stranded:
        submission.recover_from_stranding()

    if submission.status in {Submission.Status.NEW, Submission.Status.ENRICHING}:
        return render(
            request, "submissions/working.html#status", {"submission": submission}
        )

    response = render(
        request, "submissions/working.html#status", {"submission": submission}
    )
    # Tell HTMX to navigate rather than keep polling.
    response["HX-Redirect"] = reverse("submission_detail", args=[submission.pk])
    return response


@login_required
def my_submissions(request):
    submissions = Submission.objects.filter(submitted_by=request.user)
    return render(
        request,
        "submissions/mine.html",
        {"submissions": submissions, "site_config": SiteConfig.load()},
    )
