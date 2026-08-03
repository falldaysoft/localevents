"""The path from "here's a link" to a listing a moderator can act on.

The shape that matters: nothing an AI produced reaches a moderator without the
submitter having seen it first. That confirmation step is what makes a cheap,
imperfect model acceptable — a rough extraction costs the submitter a minute of
editing rather than putting plausible-looking invented details in front of
someone who will trust them.
"""

import secrets
from datetime import timedelta

from django.conf import settings
from django.db import models
from django.utils import timezone


# How long a submission may sit in "reading the page" before we assume the
# worker that claimed it is gone. Finite, because the alternative is a
# submitter watching a spinner forever — but well clear of legitimately slow
# work: measured runs against real pages came in at 116s and 336s, so a
# threshold anywhere near five minutes would start cutting off live work.
STALE_ENRICHMENT_AFTER = timedelta(minutes=20)


class SubmissionQuerySet(models.QuerySet):
    def awaiting_review(self):
        return self.filter(status=Submission.Status.PENDING_REVIEW)

    def stranded(self, now=None):
        """Submissions whose enrichment will never finish.

        A worker that dies mid-task — a deploy, an OOM, a crash — leaves its
        row claimed and never releases it. Nothing else notices, so without
        this the submission sits in "reading the page" indefinitely.
        """
        now = now or timezone.now()
        return self.filter(
            status__in=[Submission.Status.NEW, Submission.Status.ENRICHING],
            updated_at__lt=now - STALE_ENRICHMENT_AFTER,
        )

    def open_for(self, user):
        return self.filter(submitted_by=user).exclude(
            status__in=[
                Submission.Status.APPROVED,
                Submission.Status.REJECTED,
                Submission.Status.WITHDRAWN,
            ]
        )


class Submission(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Just submitted"
        ENRICHING = "enriching", "Reading the page"
        AWAITING_SUBMITTER = "awaiting_submitter", "Waiting on the submitter"
        PENDING_REVIEW = "pending_review", "Awaiting review"
        INFO_REQUESTED = "info_requested", "More information requested"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        WITHDRAWN = "withdrawn", "Withdrawn"

    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="submissions",
    )
    source_url = models.URLField(blank=True)
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.NEW
    )

    # The draft the submitter is reviewing, before it becomes a real Event.
    draft = models.JSONField(default=dict, blank=True)
    enrichment_message = models.CharField(max_length=300, blank=True)
    enrichment_failed = models.BooleanField(default=False)

    # Shown to the submitter when the link is a ticketing or social page and
    # the organiser probably has their own. Advisory only — see
    # submissions.sources for why this nudges rather than blocks.
    source_advice = models.CharField(max_length=400, blank=True)

    event = models.ForeignKey(
        "events.Event", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="submissions",
    )

    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="assigned_submissions",
    )
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="decided_submissions",
    )
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SubmissionQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self):
        return f"{self.display_title} ({self.get_status_display()})"

    @property
    def display_title(self):
        """Best available name for this submission.

        The draft is empty for anything entered by hand, so falling back to the
        event is what stops a manual submission showing as "Untitled" in the
        submitter's own list.
        """
        draft_title = (self.draft or {}).get("title")
        if draft_title:
            return draft_title
        if self.event_id and self.event:
            return self.event.title
        return self.source_url or "Untitled submission"

    @property
    def is_editable_by_submitter(self):
        return self.status in {
            self.Status.AWAITING_SUBMITTER,
            self.Status.INFO_REQUESTED,
        }

    @property
    def is_open(self):
        return self.status not in {
            self.Status.APPROVED,
            self.Status.REJECTED,
            self.Status.WITHDRAWN,
        }

    @property
    def is_stranded(self, now=None):
        """Has enrichment been claimed by a worker that is never coming back?"""
        if self.status not in {self.Status.NEW, self.Status.ENRICHING}:
            return False
        return (timezone.now() - self.updated_at) > STALE_ENRICHMENT_AFTER

    def recover_from_stranding(self):
        """Release a stuck submission to its owner with an honest explanation.

        Deliberately does not re-queue. A second attempt would most likely hit
        whatever killed the first, and the submitter is right here and able to
        fill the form in — which beats another silent wait.
        """
        self.status = self.Status.AWAITING_SUBMITTER
        self.enrichment_failed = True
        self.enrichment_message = (
            "We couldn't finish reading that page. Please fill in the details "
            "below yourself — everything you enter is kept."
        )
        self.save(
            update_fields=[
                "status", "enrichment_failed", "enrichment_message", "updated_at",
            ]
        )

    def mark_decided(self, user, status, note=""):
        self.status = status
        self.decided_by = user
        self.decided_at = timezone.now()
        self.decision_note = note
        self.save(
            update_fields=[
                "status", "decided_by", "decided_at", "decision_note", "updated_at",
            ]
        )


class SubmissionMessage(models.Model):
    """A note between a moderator and a submitter.

    Kept on the submission rather than sent as bare email so the exchange stays
    attached to the thing being discussed — a moderator picking up someone
    else's queue can see what was already asked.
    """

    submission = models.ForeignKey(
        Submission, on_delete=models.CASCADE, related_name="messages"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="submission_messages",
    )
    body = models.TextField()
    is_from_moderator = models.BooleanField(default=False)
    emailed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        who = "moderator" if self.is_from_moderator else "submitter"
        return f"{who}: {self.body[:60]}"


class ModerationAction(models.Model):
    """An append-only record of who did what.

    Volunteer moderators come and go; when a decision is questioned months
    later, "who approved this and when" needs an answer that does not depend on
    anyone's memory.
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL,
        related_name="moderation_actions",
    )
    submission = models.ForeignKey(
        Submission, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="actions",
    )
    event = models.ForeignKey(
        "events.Event", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="moderation_actions",
    )
    action = models.CharField(max_length=50)
    detail = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} by {self.actor or 'system'}"

    @classmethod
    def record(cls, actor, action, submission=None, event=None, detail=""):
        return cls.objects.create(
            actor=actor,
            action=action,
            submission=submission,
            event=event,
            detail=detail[:300],
        )


class SubmissionQuota(models.Model):
    """Per-user daily caps.

    Two separate limits because they protect different things: submissions
    protect the moderators' attention, enrichments protect the API bill. A user
    who repeatedly re-reads pages without submitting anything costs money
    without ever reaching the queue.
    """

    DEFAULT_SUBMISSIONS_PER_DAY = 10
    DEFAULT_ENRICHMENTS_PER_DAY = 25

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="quota"
    )
    submissions_per_day = models.PositiveSmallIntegerField(
        default=DEFAULT_SUBMISSIONS_PER_DAY
    )
    enrichments_per_day = models.PositiveSmallIntegerField(
        default=DEFAULT_ENRICHMENTS_PER_DAY
    )

    def __str__(self):
        return f"Quota for {self.user}"

    @classmethod
    def for_user(cls, user):
        quota, _ = cls.objects.get_or_create(user=user)
        return quota

    @staticmethod
    def _since_midnight():
        return timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)

    def submissions_today(self):
        return Submission.objects.filter(
            submitted_by=self.user, created_at__gte=self._since_midnight()
        ).count()

    def enrichments_today(self):
        from enrichment.models import EnrichmentRun

        return EnrichmentRun.objects.filter(
            submission__submitted_by=self.user,
            created_at__gte=self._since_midnight(),
        ).count()

    def may_submit(self):
        return self.submissions_today() < self.submissions_per_day

    def may_enrich(self):
        return self.enrichments_today() < self.enrichments_per_day
