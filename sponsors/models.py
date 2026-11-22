"""Paid placements: what is for sale, and each purchase.

Every purchase is held, not charged, until a moderator approves it. Stripe
Checkout runs with `capture_method=manual`, so the buyer's card is authorised
when they pay and only captured on approval; a rejection releases the hold.
Nobody is refunded, because nobody was charged — and the processing fee that
Stripe keeps on a refund is never spent on a placement the site turned down.

The catch is that a card hold lasts about seven days. An order that waits
longer than that lapses on Stripe's side and cannot be approved, which is
why the queue shows each order's deadline and housekeeping warns before it.
"""

from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils import timezone

from core.models import SingletonModel

from . import crypto

# Stripe's own figure for online card payments is seven days. A day short of
# it, so an approval pressed in the last hours is not racing the expiry.
HOLD_DAYS = 6
# When the queue starts calling an order urgent and housekeeping mails the
# moderators about it.
HOLD_WARNING = timedelta(days=2)


class SponsorConfig(SingletonModel):
    """Prices, limits and the Stripe account, editable without a redeploy.

    The Stripe keys are here rather than in the environment so that they never
    pass through a deploy or a CI variable. They are encrypted with a key
    derived from `SECRET_KEY` (see `sponsors.crypto`), never shown again once
    saved, and the admin for this model is superuser-only: whoever holds the
    secret key can move money.
    """

    enabled = models.BooleanField(
        default=False,
        verbose_name="sponsorships on sale",
        help_text="Off: the Promote buttons and the sponsor page are hidden and "
        "nothing new can be bought. Placements already approved keep running.",
    )

    stripe_secret_key_enc = models.TextField(blank=True, editable=False)
    stripe_webhook_secret_enc = models.TextField(blank=True, editable=False)
    stripe_webhook_endpoint_id = models.CharField(
        max_length=100, blank=True, editable=False
    )
    stripe_account_name = models.CharField(
        max_length=200, blank=True, editable=False,
        help_text="As Stripe reported it on the last connection test.",
    )

    currency = models.CharField(
        max_length=3, default="cad",
        help_text="Three-letter ISO code, lower case, e.g. cad or usd.",
    )
    automatic_tax = models.BooleanField(
        default=False,
        verbose_name="charge sales tax with Stripe Tax",
        help_text="Stripe works out the tax from the buyer's address and adds "
        "it at checkout. Turn on only once Stripe Tax is set up, with your "
        "registrations, in the Stripe dashboard — otherwise checkout fails.",
    )
    tax_code = models.CharField(
        max_length=40, blank=True,
        help_text="Optional Stripe product tax code (txcd_…). Blank uses the "
        "default from your Stripe Tax settings.",
    )

    promote_price = models.DecimalField(
        max_digits=8, decimal_places=2, default=Decimal("25.00"),
        verbose_name="promote an event: price",
        help_text="Before tax, for one promotion.",
    )
    promote_max_days = models.PositiveSmallIntegerField(
        default=14,
        verbose_name="promote an event: longest run (days)",
    )
    promote_daily_slots = models.PositiveSmallIntegerField(
        default=2,
        verbose_name="promote an event: promotions per day",
        help_text="How many sponsored events may run on the same day. Keeps "
        "paid placements rare enough to leave room for your own picks.",
    )

    class Meta:
        verbose_name = "sponsorship settings"
        verbose_name_plural = "sponsorship settings"

    def __str__(self):
        return "Sponsorship settings"

    @property
    def stripe_secret_key(self):
        return crypto.decrypt(self.stripe_secret_key_enc)

    @stripe_secret_key.setter
    def stripe_secret_key(self, value):
        self.stripe_secret_key_enc = crypto.encrypt(value)

    @property
    def stripe_webhook_secret(self):
        return crypto.decrypt(self.stripe_webhook_secret_enc)

    @stripe_webhook_secret.setter
    def stripe_webhook_secret(self, value):
        self.stripe_webhook_secret_enc = crypto.encrypt(value)

    @property
    def stripe_ready(self):
        """A key and a webhook: enough to take a payment and hear about it."""
        return bool(self.stripe_secret_key and self.stripe_webhook_secret)

    @property
    def on_sale(self):
        return self.enabled and self.stripe_ready

    @property
    def test_mode(self):
        key = self.stripe_secret_key
        return key.startswith(("sk_test_", "rk_test_"))

    @property
    def key_hint(self):
        """Enough of the stored key to recognise it, never enough to use it."""
        if not self.stripe_secret_key_enc:
            return "none stored"
        key = self.stripe_secret_key
        if not key:
            return "stored, but unreadable — SECRET_KEY has changed; paste it again"
        mode = "test" if self.test_mode else "live"
        return f"{mode} key ending …{key[-4:]}"


class SponsorOrderQuerySet(models.QuerySet):
    def awaiting_review(self):
        return self.filter(status=SponsorOrder.Status.AWAITING_REVIEW)

    def holding_a_slot(self):
        """Orders that count against a day's limit: approved, or about to be."""
        return self.filter(
            status__in=[
                SponsorOrder.Status.AWAITING_REVIEW,
                SponsorOrder.Status.APPROVED,
            ]
        )

    def overlapping(self, first, last):
        return self.filter(starts_on__lte=last, ends_on__gte=first)

    def running_on(self, day):
        return self.filter(
            status=SponsorOrder.Status.APPROVED, starts_on__lte=day, ends_on__gte=day
        )


class SponsorOrder(models.Model):
    """One purchase, from checkout to the end of its run."""

    class Product(models.TextChoices):
        PROMOTE_EVENT = "promote_event", "Promote an event"

    class Status(models.TextChoices):
        # Sent to Stripe Checkout and not back yet.
        CHECKOUT = "checkout", "At checkout"
        # Card held; a moderator decides.
        AWAITING_REVIEW = "awaiting_review", "Awaiting review"
        # Charged. Runs between starts_on and ends_on.
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Not accepted"
        # The buyer called it off before a decision.
        WITHDRAWN = "withdrawn", "Withdrawn"
        # Checkout was never finished.
        ABANDONED = "abandoned", "Not completed"
        # The hold ran out before anyone decided.
        LAPSED = "lapsed", "Lapsed"
        # Approved, charged, then refunded from the Stripe dashboard.
        REFUNDED = "refunded", "Refunded"

    product = models.CharField(
        max_length=20, choices=Product.choices, default=Product.PROMOTE_EVENT
    )
    status = models.CharField(
        max_length=20, choices=Status.choices, default=Status.CHECKOUT
    )
    buyer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT,
        related_name="sponsor_orders",
    )
    event = models.ForeignKey(
        "events.Event", null=True, blank=True, on_delete=models.SET_NULL,
        related_name="sponsor_orders",
    )
    starts_on = models.DateField()
    ends_on = models.DateField()
    buyer_note = models.TextField(
        blank=True, help_text="Anything the buyer wanted the editor to know."
    )

    currency = models.CharField(max_length=3)
    amount = models.DecimalField(
        max_digits=8, decimal_places=2, help_text="Before tax."
    )
    tax_amount = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True
    )
    total = models.DecimalField(
        max_digits=8, decimal_places=2, null=True, blank=True,
        help_text="What the card was held for, tax included.",
    )
    livemode = models.BooleanField(default=False)

    stripe_checkout_session_id = models.CharField(
        max_length=255, blank=True, db_index=True
    )
    stripe_payment_intent_id = models.CharField(
        max_length=255, blank=True, db_index=True
    )

    authorized_at = models.DateTimeField(null=True, blank=True)
    hold_expires_at = models.DateTimeField(null=True, blank=True)
    hold_warned_at = models.DateTimeField(null=True, blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+",
    )
    decision_note = models.TextField(
        blank=True, help_text="Sent to the buyer with the decision."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = SponsorOrderQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "starts_on", "ends_on"])]

    def __str__(self):
        return f"{self.get_product_display()} #{self.pk}"

    @property
    def days(self):
        return (self.ends_on - self.starts_on).days + 1

    @property
    def hold_urgent(self):
        return (
            self.status == self.Status.AWAITING_REVIEW
            and self.hold_expires_at is not None
            and self.hold_expires_at - timezone.now() <= HOLD_WARNING
        )

    @property
    def is_running(self):
        today = timezone.localdate()
        return (
            self.status == self.Status.APPROVED
            and self.starts_on <= today <= self.ends_on
        )

    @property
    def description(self):
        if self.event_id:
            return f"Sponsored listing: {self.event.title}"
        return self.get_product_display()

    def mark_authorized(self, now=None):
        now = now or timezone.now()
        self.status = self.Status.AWAITING_REVIEW
        self.authorized_at = now
        self.hold_expires_at = now + timedelta(days=HOLD_DAYS)
