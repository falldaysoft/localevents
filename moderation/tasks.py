"""Outbound mail for moderation decisions.

Queued rather than sent inline: SMTP is the slowest and least reliable step in
the whole flow, and a decision that is already committed must not appear to
fail because a relay timed out.

One caution specific to this project — renaming this function is a two-step
deploy. `django_tasks` resolves `task_path` with `import_string` and lets the
ImportError propagate, so a queued row pointing at the old path takes down
*every* background task, not just this one. See CLAUDE.md.
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db.models import Q
from django.tasks import task
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from submissions.models import Submission, SubmissionMessage

logger = logging.getLogger("moderation.tasks")

SUBJECTS = {
    "approved": "Your event is now listed on {site}",
    "rejected": "About your submission to {site}",
    "info_requested": "A question about your submission to {site}",
}

MODERATOR_SUBJECTS = {
    "new": "[{site}] To review: {title}",
    "resubmitted": "[{site}] Sent back for review: {title}",
}


def absolute_url(path):
    return f"{settings.SITE_BASE_URL.rstrip('/')}{path}"


@task()
def email_submitter(submission_id, kind, message_id=None):
    """Tell the submitter what happened.

    Silent on a submission that has since been deleted: the decision is
    already recorded and there is nobody left to inform, so raising would only
    leave a failed task row for someone to investigate.
    """
    try:
        submission = Submission.objects.select_related(
            "submitted_by", "event"
        ).get(pk=submission_id)
    except Submission.DoesNotExist:
        logger.warning("email_submitter: submission %s is gone", submission_id)
        return

    recipient = submission.submitted_by.email
    if not recipient:
        logger.warning("email_submitter: %s has no email address", submission_id)
        return

    message = None
    if message_id:
        message = SubmissionMessage.objects.filter(pk=message_id).first()

    context = {
        "submission": submission,
        "event": submission.event,
        "message": message,
        "site_name": settings.SITE_NAME,
        "contact_email": settings.CONTACT_EMAIL,
        "submission_url": absolute_url(
            reverse("submission_detail", args=[submission.pk])
        ),
        "event_url": (
            absolute_url(reverse("event_detail", args=[submission.event.slug]))
            if submission.event and submission.event.slug
            else ""
        ),
    }

    send_mail(
        subject=SUBJECTS[kind].format(site=settings.SITE_NAME),
        message=render_to_string(f"moderation/email/{kind}.txt", context),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[recipient],
    )

    if message is not None:
        message.emailed_at = timezone.now()
        message.save(update_fields=["emailed_at"])


@task()
def email_moderators(submission_id, kind):
    """Tell the moderators something is waiting in the queue.

    Everyone who can decide is told except the person who submitted it, who
    knows. Nothing is sent for a submission that has left the queue by the
    time the worker gets here — decided or withdrawn in the meantime — because
    a "to review" mail about a listing nobody can review is a wasted trip.
    """
    submission = (
        Submission.objects.select_related("submitted_by", "event__venue")
        .filter(pk=submission_id)
        .first()
    )
    if submission is None:
        logger.warning("email_moderators: submission %s is gone", submission_id)
        return
    if submission.status != Submission.Status.PENDING_REVIEW:
        return

    recipients = list(
        get_user_model().objects.filter(is_active=True)
        .filter(Q(is_superuser=True) | Q(groups__name="Moderators"))
        .exclude(pk=submission.submitted_by_id)
        .exclude(email="")
        .values_list("email", flat=True)
        .distinct()
    )
    if not recipients:
        return

    event = submission.event
    context = {
        "submission": submission,
        "event": event,
        "occurrences": list(event.occurrences.order_by("start")) if event else [],
        "resubmitted": kind == "resubmitted",
        "site_name": settings.SITE_NAME,
        "review_url": absolute_url(reverse("mod_submission", args=[submission.pk])),
        "queue_url": absolute_url(reverse("mod_queue")),
    }
    subject = MODERATOR_SUBJECTS[kind].format(
        site=settings.SITE_NAME, title=submission.display_title
    )
    body = render_to_string("moderation/email/mod_new.txt", context)
    # One message each rather than one To line: moderators are volunteers who
    # have not agreed to be shown each other's addresses.
    for recipient in recipients:
        send_mail(
            # A header cannot carry a line break, and a title is a stranger's text.
            subject=" ".join(subject.split()),
            message=body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient],
        )
