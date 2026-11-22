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
from django.db import DatabaseError, transaction
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
        orders.approved_or_approving() if approved_only
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


def sync_checkout(order):
    """Bring an order at checkout up to date with Stripe.

    Called from the buyer's return page and from the webhook, whichever comes
    first. Stripe is read *before* any row is locked — see "Money calls" below
    — and the row lock afterwards means the second caller finds the work done
    and the "we've got it" mail goes once.
    """
    if order.status != Status.CHECKOUT or not order.stripe_checkout_session_id:
        return order
    info = payments.read_checkout(order.stripe_checkout_session_id)
    with transaction.atomic():
        order = _locked(order.pk)
        if order.status != Status.CHECKOUT:
            return order
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


def withdraw(order, buyer):
    """The buyer calls it off before a decision; the hold is released."""
    if order.buyer_id != buyer.pk:
        raise SponsorError("That is not your order.")
    if order.status == Status.CHECKOUT:
        if order.stripe_checkout_session_id:
            try:
                payments.expire_checkout(order.stripe_checkout_session_id)
            except stripe.StripeError:
                pass  # Already finished or expired; nothing to stop.
        with transaction.atomic():
            order = _locked(order.pk)
            if order.status == Status.CHECKOUT:
                order.status = Status.ABANDONED
                order.decided_at = timezone.now()
                order.save()
        return order

    def check(o):
        if o.status != Status.AWAITING_REVIEW:
            raise SponsorError("This order has already been decided.")

    order = _claim(order.pk, Status.WITHDRAWN, check=check)
    _release_or_unclaim(order)
    return _finish_quietly(order)


# ---------------------------------------------------------------------------
# Money calls
#
# A Stripe call never runs inside a database transaction. It cannot be rolled
# back, so a transaction that charged a card and then failed to commit — a
# SQLite "database is locked", a dropped connection — took the money and lost
# the decision; that happened in the first local test. It would also hold a
# write lock for the length of a network round trip.
#
# So every decision is three steps: claim the order (status PROCESSING, with
# the outcome, the moderator and the note recorded and committed), call
# Stripe, then finish (final status, audit row, buyer's mail). If Stripe
# refuses, the claim is undone and nothing happened. If finishing fails, the
# claim is already on disk, and the webhook Stripe sends for the same change —
# or housekeeping, failing that — finishes it with the moderator's name and
# note intact.
# ---------------------------------------------------------------------------


def _locked(pk):
    return SponsorOrder.objects.select_for_update().select_related("event").get(pk=pk)


def _claim(pk, outcome, *, moderator=None, note="", check=None):
    with transaction.atomic():
        order = _locked(pk)
        if check is not None:
            check(order)
        order.status = Status.PROCESSING
        order.pending_status = outcome
        order.decided_at = timezone.now()
        if moderator is not None:
            order.decided_by = moderator
        if note:
            order.decision_note = note
        order.save()
    return order


def _unclaim(order):
    """Stripe said no: put the order back exactly as the claim found it."""
    with transaction.atomic():
        order = _locked(order.pk)
        if order.status == Status.PROCESSING:
            order.status = Status.AWAITING_REVIEW
            order.pending_status = ""
            order.decided_at = None
            order.decided_by = None
            order.decision_note = ""
            order.save()
    return order


def finish(order):
    """Apply a claimed outcome: the status, the audit row, the buyer's mail.

    Idempotent — only an order still PROCESSING changes — so the action that
    claimed it, the webhook and housekeeping can all call it safely.
    """
    with transaction.atomic():
        order = _locked(order.pk)
        if order.status != Status.PROCESSING:
            return order
        outcome = order.pending_status
        order.status = outcome
        order.pending_status = ""
        order.save()
        actor = order.decided_by
        if outcome == Status.APPROVED:
            detail = (
                f"{order} · {order.starts_on:%-d %b}–{order.ends_on:%-d %b} · "
                f"{order.total or order.amount} {order.currency.upper()}"
            )
            if actor is None:
                detail += " · captured outside the queue"
            ModerationAction.record(actor, "sponsor_approved", event=order.event, detail=detail)
            _mail_buyer(order, "approved")
        elif outcome == Status.REJECTED:
            ModerationAction.record(
                actor, "sponsor_rejected", event=order.event,
                detail=f"{order}: {order.decision_note}",
            )
            _mail_buyer(order, "rejected")
        elif outcome == Status.LAPSED:
            ModerationAction.record(
                None, "sponsor_lapsed", event=order.event,
                detail=f"{order}: the card hold ran out before a decision",
            )
            _mail_buyer(order, "lapsed")
    return order


def _finish_quietly(order):
    """`finish`, leaving a failure for the webhook or housekeeping.

    The money has moved by the time this runs, so a database error here must
    not be shown as "nothing happened". The order stays PROCESSING, which the
    queue shows, and is finished from what the claim recorded.
    """
    try:
        return finish(order)
    except DatabaseError:
        logger.exception("sponsor order %s: Stripe done, record not finished yet", order.pk)
        order.refresh_from_db()
        return order


def _release_or_unclaim(order):
    try:
        _release(order)
    except SponsorError:
        _unclaim(order)
        raise


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def approve(order, moderator, note=""):
    """Charge the held card and start the run."""

    def check(o):
        if o.status != Status.AWAITING_REVIEW:
            raise SponsorError(f"This order is {o.get_status_display().lower()}, not awaiting review.")
        if o.ends_on < timezone.localdate():
            raise SponsorError("Its run has already ended. Reject it to release the hold.")
        if o.event_id and not promotable(o.event):
            raise SponsorError("The event is no longer published. Reject it, or republish the event first.")
        full = full_days(
            max(o.starts_on, timezone.localdate()), o.ends_on,
            exclude=o.pk, approved_only=True,
        )
        if full:
            raise SponsorError(
                "Approving this would go over the daily limit on "
                + ", ".join(day.strftime("%a %-d %b") for day in full)
                + ". Reject it, or raise the limit in the admin."
            )

    order = _claim(order.pk, Status.APPROVED, moderator=moderator, note=note, check=check)
    try:
        status = payments.capture(order.stripe_payment_intent_id)
    except stripe.StripeError as exc:
        _unclaim(order)
        raise SponsorError(
            f"Stripe would not take the payment: {exc.user_message or exc}"
        ) from exc
    if status != "succeeded":
        _unclaim(order)
        raise SponsorError(f"Stripe reports the payment as {status}; not approved.")
    return _finish_quietly(order)


def reject(order, moderator, reason):
    """Release the hold and tell the buyer why."""
    if not reason.strip():
        raise SponsorError("Say why, briefly. The buyer gets this with the news.")

    def check(o):
        if o.status != Status.AWAITING_REVIEW:
            raise SponsorError(f"This order is {o.get_status_display().lower()}, not awaiting review.")

    order = _claim(order.pk, Status.REJECTED, moderator=moderator, note=reason, check=check)
    _release_or_unclaim(order)
    return _finish_quietly(order)


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


RELEASED = (Status.REJECTED, Status.WITHDRAWN, Status.LAPSED)


def _settle(order, intent_status):
    """Bring an order in line with what Stripe says its payment now is.

    Shared by the webhook and housekeeping. A claimed order is finished; one
    still awaiting review whose payment moved anyway (captured or cancelled in
    the Stripe dashboard, or the hold running out) is claimed on the spot
    with no moderator, so it gets the same audit row and mail as any other.
    """
    if intent_status == "succeeded":
        if order.status == Status.AWAITING_REVIEW:
            # Money taken means the placement is owed.
            order = _claim(order.pk, Status.APPROVED, check=_still(Status.AWAITING_REVIEW))
        if order.status == Status.PROCESSING and order.pending_status == Status.APPROVED:
            return finish(order)
    elif intent_status == "canceled":
        if order.status == Status.AWAITING_REVIEW:
            order = _claim(order.pk, Status.LAPSED, check=_still(Status.AWAITING_REVIEW))
        elif order.status == Status.PROCESSING and order.pending_status == Status.APPROVED:
            # The capture never landed and the hold is gone.
            with transaction.atomic():
                locked = _locked(order.pk)
                if locked.status == Status.PROCESSING:
                    locked.pending_status = Status.LAPSED
                    locked.save(update_fields=["pending_status", "updated_at"])
            order.refresh_from_db()
        if order.status == Status.PROCESSING and order.pending_status in RELEASED:
            return finish(order)
    elif intent_status == "requires_capture" and order.status == Status.PROCESSING:
        # The Stripe step never happened: a capture to undo, or a release to retry.
        if order.pending_status == Status.APPROVED:
            return _unclaim(order)
        _release(order)
        return finish(order)
    return order


def _still(status):
    def check(o):
        if o.status != status:
            raise SponsorError("Already changed.")
    return check


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

    intent_id = obj.get("id") if kind.startswith("payment_intent.") else obj.get("payment_intent")
    order = _order_for_intent(intent_id)
    if order is None:
        return

    try:
        if kind == "payment_intent.succeeded":
            _settle(order, "succeeded")
        elif kind == "payment_intent.canceled":
            _settle(order, "canceled")
        elif kind == "charge.refunded" and obj.get("refunded"):
            with transaction.atomic():
                order = _locked(order.pk)
                if order.status == Status.APPROVED:
                    order.status = Status.REFUNDED
                    order.save(update_fields=["status", "updated_at"])
                    ModerationAction.record(
                        None, "sponsor_refunded", event=order.event,
                        detail=f"{order}: refunded in Stripe; the placement has stopped",
                    )
    except SponsorError:
        pass  # Someone else changed it between the read and the claim.


# A claim normally finishes within the same request. One older than this was
# interrupted, and Stripe is asked what actually happened.
STUCK_AFTER = timedelta(minutes=10)


def housekeeping(now=None):
    """Tidy what the webhook may have missed, and warn before holds run out.

    Returns a short summary for the housekeeping log.
    """
    now = now or timezone.now()
    done = {"abandoned": 0, "lapsed": 0, "warned": 0, "settled": 0}

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

    for order in SponsorOrder.objects.filter(
        status=Status.PROCESSING, updated_at__lt=now - STUCK_AFTER
    ):
        try:
            _settle(order, payments.read_intent_status(order.stripe_payment_intent_id))
        except (stripe.StripeError, payments.NotConfigured, SponsorError):
            logger.exception("could not settle sponsor order %s", order.pk)
            continue
        done["settled"] += 1

    for order in SponsorOrder.objects.awaiting_review().filter(hold_expires_at__lte=now):
        try:
            order = _claim(order.pk, Status.LAPSED, check=_still(Status.AWAITING_REVIEW))
        except SponsorError:
            continue
        try:
            _release_or_unclaim(order)
        except SponsorError:
            logger.exception("could not release lapsed sponsor order %s", order.pk)
            continue
        _finish_quietly(order)
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
