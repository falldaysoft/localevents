"""Background work for submissions.

Enrichment runs off the request path because it makes two network calls — a
page fetch and possibly a model — and neither belongs in front of someone
waiting on a form. The submitter sees a "reading the page" state that polls.
"""

import logging

from django.db import transaction
from django.tasks import task

from enrichment.pipeline import enrich_text, enrich_url

from .models import Submission

logger = logging.getLogger("submissions.tasks")


@task()
def enrich_submission(submission_id):
    """Read the submitted URL and park the draft for the submitter to check.

    Never raises. A submission that could not be enriched still has to reach
    its owner — with an explanation and an empty form.
    """
    try:
        submission = Submission.objects.get(pk=submission_id)
    except Submission.DoesNotExist:
        logger.warning("enrich_submission: %s no longer exists", submission_id)
        return

    if submission.status != Submission.Status.NEW:
        return

    submission.status = Submission.Status.ENRICHING
    submission.save(update_fields=["status", "updated_at"])

    try:
        if submission.pasted_text:
            # Pasted text wins over the link. The submitter pasted it because
            # the link cannot be read, and fetching it anyway would only spend
            # a request on the login wall that sent them here.
            result = enrich_text(
                submission.pasted_text, submission.source_url,
                submission=submission,
            )
        else:
            result = enrich_url(submission.source_url, submission=submission)
    except Exception:
        # Belt and braces: the pipeline is written not to raise, but a crash
        # here would strand the submission in "reading the page" forever.
        logger.exception("enrichment crashed for submission %s", submission_id)
        submission.draft = submission.fallback_draft()
        submission.enrichment_failed = True
        submission.enrichment_message = (
            "Something went wrong reading that page. Please fill in the "
            "details below yourself."
        )
        submission.status = Submission.Status.AWAITING_SUBMITTER
        _park(submission)
        return

    submission.draft = (
        result.draft.model_dump(mode="json")
        if result.draft
        else submission.fallback_draft()
    )
    submission.enrichment_failed = result.failed
    submission.enrichment_message = result.message[:300]
    submission.status = Submission.Status.AWAITING_SUBMITTER
    _park(submission)


def _park(submission):
    """Save the finished read, unless its owner withdrew it meanwhile.

    A read takes minutes, and withdrawing is allowed while it runs. A plain
    save at the end would write AWAITING_SUBMITTER over WITHDRAWN and put the
    submission back in its owner's list as if they had never taken it back.
    """
    with transaction.atomic():
        current = (
            Submission.objects.select_for_update()
            .filter(pk=submission.pk)
            .values_list("status", flat=True)
            .first()
        )
        if current in (None, Submission.Status.WITHDRAWN):
            return False
        submission.save()
    return True


@task()
def triage_inbound_email(inbound_id, force=False):
    """Read one stored email and queue whatever events it holds.

    Renaming this is a two-step deploy — see CLAUDE.md on stale task paths.
    """
    from .inbound import process
    from .models import InboundEmail

    try:
        inbound = InboundEmail.objects.get(pk=inbound_id)
    except InboundEmail.DoesNotExist:
        logger.warning("triage_inbound_email: %s no longer exists", inbound_id)
        return
    # A forced re-read may start from any verdict; an ordinary one only from
    # a fresh message, so a duplicate enqueue cannot queue an event twice.
    if not force and inbound.status != InboundEmail.Status.RECEIVED:
        return
    try:
        process(inbound, force=force)
    except Exception:
        logger.exception("triage crashed for inbound email %s", inbound_id)
        inbound.status = InboundEmail.Status.FAILED
        inbound.triage_reason = "Something went wrong reading this email."
        inbound.save(update_fields=["status", "triage_reason", "updated_at"])
