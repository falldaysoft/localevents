"""Sponsorship decisions, kept out of the views like moderation's are.

The order row, the money and the mail must move together: a view that
captured the payment and forgot to record it, or recorded a rejection and
left the hold on the buyer's card, would look fine in review. Every state
change goes through here, and mail is queued with `transaction.on_commit` so
a decision that rolls back never tells anyone anything.
"""

import logging
from datetime import timedelta

import stripe
from django.db import transaction
from django.utils import timezone

from events.models import Event
from submissions.models import ModerationAction

from . import payments
from .models import HOLD_WARNING, SponsorConfig, SponsorOrder

logger = logging.getLogger("sponsors")

Status = SponsorOrder.Status


class SponsorError(Exception):
    """Something the person pressing the button should be told, in words."""


# ---------------------------------------------------------------------------
# What is for sale
# ---------------------------------------------------------------------------


def promotable(event):
    """Only listings already on the site can be promoted.

    Payment buys placement, never admission: the listing has passed review on
    its own merits before anyone can pay to move it up.
    """
    return event.status == Event.Status.PUBLISHED


def last_upcoming_day(event, now=None):
    occurrences = list(event.upcoming_occurrences())
    if not occurrences:
        return None
    last = occurrences[-1]
    return timezone.localtime(last.end or last.start).date()


def promote_window(event, starts_on, config=None, now=None):
    """(first, last) day of a promotion starting on `starts_on`.

    It runs for the configured number of days or until the event's last
    upcoming date, whichever comes first — promoting a concert for a week
    after it has happened would sell a buyer nothing.
    """
    config = config or SponsorConfig.load()
    today = timezone.localdate(now)
    if starts_on < today:
        raise SponsorError("The start date has already passed.")
    last_day = last_upcoming_day(event, now)
    if last_day is None:
        raise SponsorError("This event has no dates still to come.")
    if starts_on > last_day:
        raise SponsorError("That is after the event's last date.")
    ends_on = min(starts_on + timedelta(days=config.promote_max_days - 1), last_day)
    return starts_on, ends_on


def full_days(first, last, config=None, exclude=None, approved_only=False):
    """The days between `first` and `last` that have no promotion slot left."""
    config = config or SponsorConfig.load()
    orders = SponsorOrder.objects.filter(
        product=SponsorOrder.Product.PROMOTE_EVENT
    ).overlapping(first, last)
    orders = (
        orders.filter(status=Status.APPROVED) if approved_only
        else orders.holding_a_slot()
    )
    if exclude is not None:
        orders = orders.exclude(pk=exclude)
    spans = list(orders.values_list("starts_on", "ends_on"))
    full = []
    day = first
    while day <= last:
        taken = sum(1 for start, end in spans if start <= day <= end)
        if taken >= config.promote_daily_slots:
            full.append(day)
        day += timedelta(days=1)
    return full


def promoted_event_ids(now=None):
    """Events with an approved promotion running today."""
    day = timezone.localdate(now)
    return set(
        SponsorOrder.objects.running_on(day)
        .filter(product=SponsorOrder.Product.PROMOTE_EVENT, event__isnull=False)
        .values_list("event_id", flat=True)
    )


# ---------------------------------------------------------------------------
# Buying
# ---------------------------------------------------------------------------


def start_promotion(event, buyer, starts_on, note="", now=None):
    """Create the order and open Stripe Checkout; returns (order, checkout url)."""
    config = SponsorConfig.load()
    if not config.on_sale:
        raise SponsorError("Sponsorships are not on sale at the moment.")
    if not promotable(event):
        raise SponsorError("Only events already listed on the site can be promoted.")
    first, last = promote_window(event, starts_on, config, now)
    full = full_days(first, last, config)
    if full:
        listed = ", ".join(day.strftime("%a %-d %b") for day in full)
        raise SponsorError(
            f"Promotion spots are taken on {listed}. Try a later start date."
        )
    order = SponsorOrder.objects.create(
        product=SponsorOrder.Product.PROMOTE_EVENT,
        buyer=buyer,
        event=event,
        starts_on=first,
        ends_on=last,
        buyer_note=note,
        currency=config.currency,
        amount=config.promote_price,
        livemode=not config.test_mode,
    )
    try:
        session_id, url = payments.create_checkout(order, config)
    except (stripe.StripeError, payments.NotConfigured) as exc:
        logger.exception("checkout for sponsor order %s failed", order.pk)
        order.status = Status.ABANDONED
        order.save(update_fields=["status", "updated_at"])
        raise SponsorError(
            "The payment page could not be opened. Nothing was charged; "
            "please try again in a few minutes."
        ) from exc
    order.stripe_checkout_session_id = session_id
    order.save(update_fields=["stripe_checkout_session_id", "updated_at"])
    return order, url


@transaction.atomic
def sync_checkout(order):
    """Bring an order at checkout up to date with Stripe.

    Called from the buyer's return page and from the webhook, whichever comes
    first; the row lock means the second finds the work done and the "we've
    got it" mail goes once.
    """
    order = SponsorOrder.objects.select_for_update().get(pk=order.pk)
    if order.status != Status.CHECKOUT or not order.stripe_checkout_session_id:
        return order
    info = payments.read_checkout(order.stripe_checkout_session_id)
    if info["intent_status"] == "requires_capture":
        order.mark_authorized()
        order.stripe_payment_intent_id = info["payment_intent_id"]
        order.tax_amount = info["amount_tax"]
        order.total = info["amount_total"]
        order.livemode = info["livemode"]
        order.save()
        _mail_buyer(order, "received")
        _mail_moderators(order, "new")
    elif info["session_status"] == "expired":
        order.status = Status.ABANDONED
        order.save(update_fields=["status", "updated_at"])
    return order


@transaction.atomic
def withdraw(order, buyer):
    """The buyer calls it off before a decision; the hold is released."""
    order = SponsorOrder.objects.select_for_update().get(pk=order.pk)
    if order.buyer_id != buyer.pk:
        raise SponsorError("That is not your order.")
    if order.status == Status.CHECKOUT:
        if order.stripe_checkout_session_id:
            try:
                payments.expire_checkout(order.stripe_checkout_session_id)
            except stripe.StripeError:
                pass  # Already finished or expired; nothing to stop.
        order.status = Status.ABANDONED
    elif order.status == Status.AWAITING_REVIEW:
        _release(order)
        order.status = Status.WITHDRAWN
    else:
        raise SponsorError("This order has already been decided.")
    order.decided_at = timezone.now()
    order.save()
    return order


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


@transaction.atomic
def approve(order, moderator, note=""):
    order = SponsorOrder.objects.select_for_update().select_related("event").get(pk=order.pk)
    if order.status != Status.AWAITING_REVIEW:
        raise SponsorError(f"This order is {order.get_status_display().lower()}, not awaiting review.")
    if order.ends_on < timezone.localdate():
        raise SponsorError("Its run has already ended. Reject it to release the hold.")
    if order.event_id and not promotable(order.event):
        raise SponsorError("The event is no longer published. Reject it, or republish the event first.")
    full = full_days(
        max(order.starts_on, timezone.localdate()), order.ends_on,
        exclude=order.pk, approved_only=True,
    )
    if full:
        raise SponsorError(
            "Approving this would go over the daily limit on "
            + ", ".join(day.strftime("%a %-d %b") for day in full)
            + ". Reject it, or raise the limit in the admin."
        )
    try:
        status = payments.capture(order.stripe_payment_intent_id)
    except stripe.StripeError as exc:
        raise SponsorError(
            f"Stripe would not take the payment: {exc.user_message or exc}"
        ) from exc
    if status != "succeeded":
        raise SponsorError(f"Stripe reports the payment as {status}; not approved.")
    order.status = Status.APPROVED
    order.decided_at = timezone.now()
    order.decided_by = moderator
    order.decision_note = note
    order.save()
    ModerationAction.record(
        moderator, "sponsor_approved", event=order.event,
        detail=f"{order} · {order.starts_on:%-d %b}–{order.ends_on:%-d %b} · {order.total or order.amount} {order.currency.upper()}",
    )
    _mail_buyer(order, "approved")
    return order


@transaction.atomic
def reject(order, moderator, reason):
    order = SponsorOrder.objects.select_for_update().select_related("event").get(pk=order.pk)
    if order.status != Status.AWAITING_REVIEW:
        raise SponsorError(f"This order is {order.get_status_display().lower()}, not awaiting review.")
    if not reason.strip():
        raise SponsorError("Say why, briefly. The buyer gets this with the news.")
    _release(order)
    order.status = Status.REJECTED
    order.decided_at = timezone.now()
    order.decided_by = moderator
    order.decision_note = reason
    order.save()
    ModerationAction.record(
        moderator, "sponsor_rejected", event=order.event, detail=f"{order}: {reason}"
    )
    _mail_buyer(order, "rejected")
    return order


def _release(order):
    if not order.stripe_payment_intent_id:
        return
    try:
        payments.release(order.stripe_payment_intent_id)
    except stripe.InvalidRequestError:
        # Already cancelled — the hold lapsed on Stripe's side, or someone
        # cancelled it in the dashboard. Either way there is nothing to free.
        logger.info("payment %s was already released", order.stripe_payment_intent_id)
    except stripe.StripeError as exc:
        raise SponsorError(
            f"Stripe would not release the hold: {exc.user_message or exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Webhooks and housekeeping
# ---------------------------------------------------------------------------


def _order_for(obj):
    pk = (obj.get("metadata") or {}).get("sponsor_order")
    if pk and str(pk).isdigit():
        return SponsorOrder.objects.filter(pk=int(pk)).first()
    return None


def _order_for_intent(intent_id):
    if not intent_id:
        return None
    return SponsorOrder.objects.filter(stripe_payment_intent_id=intent_id).first()


def handle_webhook(event):
    """Apply one verified Stripe event. Unknown types are ignored.

    Every branch is idempotent: Stripe delivers at least once, and the
    buyer's return page may already have done the same work.
    """
    kind = event.type
    # A plain dict: Stripe's objects raise on `.get` and on missing keys.
    obj = event.data.object.to_dict()

    if kind in ("checkout.session.completed", "checkout.session.expired"):
        order = _order_for(obj)
        if order is not None:
            sync_checkout(order)
        return

    if kind == "payment_intent.amount_capturable_updated":
        order = _order_for(obj) or _order_for_intent(obj.get("id"))
        if order is not None:
            sync_checkout(order)
        return

    with transaction.atomic():
        intent_id = obj.get("id") if kind.startswith("payment_intent.") else obj.get("payment_intent")
        order = _order_for_intent(intent_id)
        if order is None:
            return
        order = SponsorOrder.objects.select_for_update().get(pk=order.pk)

        if kind == "payment_intent.canceled" and order.status == Status.AWAITING_REVIEW:
            order.status = Status.LAPSED
            order.save(update_fields=["status", "updated_at"])
            _mail_buyer(order, "lapsed")
        elif kind == "payment_intent.succeeded" and order.status == Status.AWAITING_REVIEW:
            # Captured somewhere other than the queue — the Stripe dashboard.
            # Money taken means the placement is owed.
            order.status = Status.APPROVED
            order.decided_at = timezone.now()
            order.save(update_fields=["status", "decided_at", "updated_at"])
        elif kind == "charge.refunded" and order.status == Status.APPROVED:
            if obj.get("refunded"):
                order.status = Status.REFUNDED
                order.save(update_fields=["status", "updated_at"])


def housekeeping(now=None):
    """Tidy what the webhook may have missed, and warn before holds run out.

    Returns a short summary for the housekeeping log.
    """
    now = now or timezone.now()
    done = {"abandoned": 0, "lapsed": 0, "warned": 0}

    # Stripe Checkout sessions expire after 24 hours.
    for order in SponsorOrder.objects.filter(
        status=Status.CHECKOUT, created_at__lt=now - timedelta(hours=25)
    ):
        try:
            order = sync_checkout(order)
        except (stripe.StripeError, payments.NotConfigured):
            logger.exception("could not read checkout for sponsor order %s", order.pk)
        if order.status == Status.CHECKOUT:
            order.status = Status.ABANDONED
            order.save(update_fields=["status", "updated_at"])
            done["abandoned"] += 1

    for order in SponsorOrder.objects.awaiting_review().filter(hold_expires_at__lte=now):
        with transaction.atomic():
            order = SponsorOrder.objects.select_for_update().get(pk=order.pk)
            if order.status != Status.AWAITING_REVIEW:
                continue
            try:
                _release(order)
            except SponsorError:
                logger.exception("could not release lapsed sponsor order %s", order.pk)
                continue
            order.status = Status.LAPSED
            order.save(update_fields=["status", "updated_at"])
            _mail_buyer(order, "lapsed")
            done["lapsed"] += 1

    for order in SponsorOrder.objects.awaiting_review().filter(
        hold_warned_at__isnull=True, hold_expires_at__lte=now + HOLD_WARNING
    ):
        order.hold_warned_at = now
        order.save(update_fields=["hold_warned_at", "updated_at"])
        _mail_moderators(order, "hold_ending")
        done["warned"] += 1

    return done


# ---------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------


def _mail_buyer(order, kind):
    from .tasks import email_buyer

    transaction.on_commit(lambda: email_buyer.enqueue(order.pk, kind))


def _mail_moderators(order, kind):
    from .tasks import email_moderators

    transaction.on_commit(lambda: email_moderators.enqueue(order.pk, kind))
