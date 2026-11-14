import secrets

from django.conf import settings
from django.db import models
from django.utils import timezone


def new_token():
    return secrets.token_urlsafe(24)


def normalize_email(email):
    """One address, one row, however it was typed.

    Lowercased whole, not just the domain as Django's own normaliser does: the
    local part is case-sensitive in theory and in practice no provider treats
    it so, and two rows for one inbox would mean two copies of every email.
    """
    return (email or "").strip().lower()


class SubscriberQuerySet(models.QuerySet):
    def active(self):
        return self.filter(confirmed_at__isnull=False, unsubscribed_at__isnull=True)

    def pending(self):
        return self.filter(confirmed_at__isnull=True, unsubscribed_at__isnull=True)


class Subscriber(models.Model):
    """One address that has asked for, or once asked for, the weekly email.

    A row outlives an unsubscribe on purpose: `unsubscribed_at` is the record
    that this person said stop, and signing up again has to go through a fresh
    confirmation rather than quietly reviving the old consent.

    `user` and `fingerprint` are how the email finds the reader's own marks —
    the account when there is one, otherwise the anonymous `visitor` cookie the
    signup form was submitted from (see `events.interest`). No IP is stored.
    """

    class Source(models.TextChoices):
        FORM = "form", "Signup form"
        PROFILE = "profile", "Account profile"
        MODERATOR = "moderator", "Added by a moderator"

    email = models.EmailField(unique=True)
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="digest_subscription",
    )
    fingerprint = models.CharField(
        max_length=64,
        blank=True,
        help_text="HMAC of the visitor cookie the signup came from, for an "
        "anonymous reader's marks.",
    )
    # In every unsubscribe link, so it must be unguessable; unlike a
    # confirmation link it never expires, because a link in a months-old email
    # still has to work.
    token = models.CharField(max_length=64, unique=True, default=new_token)
    source = models.CharField(max_length=20, choices=Source.choices)
    consent_note = models.TextField(
        blank=True,
        help_text="How and when this address agreed to receive the email.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    confirmation_sent_at = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    # The email whose link they unsubscribed from, when it was one — what
    # lets the send log say how many people a given week drove away.
    unsubscribed_via = models.ForeignKey(
        "DigestDelivery",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="unsubscribes",
    )

    objects = SubscriberQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.email

    def save(self, *args, **kwargs):
        self.email = normalize_email(self.email)
        super().save(*args, **kwargs)

    @property
    def is_active(self):
        return self.confirmed_at is not None and self.unsubscribed_at is None

    @property
    def status(self):
        if self.unsubscribed_at:
            return "unsubscribed"
        if self.confirmed_at:
            return "active"
        return "pending"

    def _note(self, note, now):
        if note:
            stamp = timezone.localtime(now).strftime("%Y-%m-%d %H:%M")
            self.consent_note = "\n".join(
                filter(None, [self.consent_note, f"{stamp}: {note}"])
            )

    def activate(self, note=""):
        """Confirmed and receiving, from now. Idempotent on an active row."""
        if self.is_active:
            return
        now = timezone.now()
        self.confirmed_at = now
        self.unsubscribed_at = None
        self._note(note, now)
        self.save(update_fields=["confirmed_at", "unsubscribed_at", "consent_note"])

    def deactivate(self, via=None, note=""):
        if self.unsubscribed_at:
            return
        self.unsubscribed_at = timezone.now()
        self.unsubscribed_via = via
        self._note(note, self.unsubscribed_at)
        self.save(update_fields=["unsubscribed_at", "unsubscribed_via", "consent_note"])


class DigestRun(models.Model):
    """One week's sending: who was due it, and when it started and finished.

    `send_date` is unique, and creating the row is how a sender claims the
    week — a second housekeeping run in the same hour, or the next hour's,
    finds it and leaves the sending to whoever made it.
    """

    send_date = models.DateField(unique=True)
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    # Active subscribers when the run began, for the log's trend line; the
    # run's own deliveries are the people it actually covered.
    active_subscribers = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-send_date"]

    def __str__(self):
        return f"Weekly email {self.send_date}"


class DigestDelivery(models.Model):
    """One reader's email in one run, and what happened to it.

    Unique per run and reader, and only ever moved out of PENDING under a
    row lock, so a batch run twice — a worker restart, a re-queued batch —
    cannot send anyone the same week twice. The address is copied so the log
    still says who it went to after the subscriber row is gone.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting"
        SENT = "sent", "Sent"
        SKIPPED = "skipped", "Skipped"
        FAILED = "failed", "Failed"

    run = models.ForeignKey(
        DigestRun, on_delete=models.CASCADE, related_name="deliveries"
    )
    subscriber = models.ForeignKey(
        Subscriber, null=True, on_delete=models.SET_NULL, related_name="deliveries"
    )
    email = models.EmailField()
    status = models.CharField(
        max_length=10, choices=Status.choices, default=Status.PENDING
    )
    # Why a delivery was skipped or failed: a reason a moderator can read, or the
    # exception's.
    detail = models.CharField(max_length=300, blank=True)
    subject = models.CharField(max_length=300, blank=True)
    event_count = models.PositiveSmallIntegerField(default=0)
    favorites_count = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["email"]
        constraints = [
            models.UniqueConstraint(
                fields=["run", "subscriber"], name="one_delivery_per_run_and_reader"
            )
        ]

    def __str__(self):
        return f"{self.run.send_date} → {self.email}"
