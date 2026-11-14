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

    def activate(self, note=""):
        """Confirmed and receiving, from now. Idempotent on an active row."""
        if self.is_active:
            return
        now = timezone.now()
        self.confirmed_at = now
        self.unsubscribed_at = None
        if note:
            stamp = timezone.localtime(now).strftime("%Y-%m-%d %H:%M")
            self.consent_note = "\n".join(
                filter(None, [self.consent_note, f"{stamp}: {note}"])
            )
        self.save(update_fields=["confirmed_at", "unsubscribed_at", "consent_note"])

    def deactivate(self):
        if self.unsubscribed_at:
            return
        self.unsubscribed_at = timezone.now()
        self.save(update_fields=["unsubscribed_at"])
