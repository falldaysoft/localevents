"""Background work for submissions.

Enrichment runs off the request path because it makes two network calls — a
page fetch and possibly a model — and neither belongs in front of someone
waiting on a form. The submitter sees a "reading the page" state that polls.
"""

import logging

from django.tasks import task

from enrichment.pipeline import enrich_url

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
        result = enrich_url(submission.source_url, submission=submission)
    except Exception:
        # Belt and braces: the pipeline is written not to raise, but a crash
        # here would strand the submission in "reading the page" forever.
        logger.exception("enrichment crashed for submission %s", submission_id)
        submission.draft = {}
        submission.enrichment_failed = True
        submission.enrichment_message = (
            "Something went wrong reading that page. Please fill in the "
            "details below yourself."
        )
        submission.status = Submission.Status.AWAITING_SUBMITTER
        submission.save()
        return

    submission.draft = result.draft.model_dump(mode="json") if result.draft else {}
    submission.enrichment_failed = result.failed
    submission.enrichment_message = result.message[:300]
    submission.status = Submission.Status.AWAITING_SUBMITTER
    submission.save()
