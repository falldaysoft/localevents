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


class AlreadyCharged(SponsorError):
    """A release was asked for, and Stripe had already taken the money."""


class OutcomeUnknown(SponsorError):
    """Stripe was asked and did not answer, and cannot be asked what happened.

    The claim is left in place rather than undone: the call may have landed,
    and the webhook or housekeeping finishes the order with the moderator and
    the note intact once Stripe can say. Undoing it lost both, and a cancel
    that had landed then came back as "lapsed" with an apology for not
    deciding in time.
    """


# Everything a call into `payments` can raise that means "Stripe did not do
# it": a refusal, an outage, or no readable key to ask with.
STRIPE_FAILURES = (stripe.StripeError, payments.NotConfigured)


def _definite(exc):
    """A refusal, as opposed to silence: Stripe answered and did nothing."""
    return isinstance(exc, (
        stripe.InvalidRequestError, stripe.CardError, stripe.AuthenticationError,
        stripe.PermissionError, payments.NotConfigured,
    ))


def _stripe_said(exc):
    if isinstance(exc, payments.NotConfigured):
        return (
            "no readable Stripe secret key is stored. Paste the key again in "
            "the sponsorship settings."
        )
    return getattr(exc, "user_message", None) or str(exc)


APPROVE_PERMISSION = "sponsors.approve_sponsororder"


# ---------------------------------------------------------------------------
# What is for sale
# ---------------------------------------------------------------------------


def promotable(event):
    """Only listings already on the site can be promoted.

    Payment buys placement, never admission: the listing has passed review on
    its own merits before anyone can pay to move it up.
    """
    return event.status == Event.Status.PUBLISHED


def buyer_relation(event, user):
    """How `user` is tied to `event`, in words, or "" for not at all."""
    if event is None or not getattr(user, "is_authenticated", False):
        return ""
    if event.submitted_by_id == user.pk:
        return "listed this event"
    if event.organizer_id and event.organizer.owner_id == user.pk:
        return "runs its organiser"
    return ""


def may_promote(event, user):
    """Only the person who listed an event, or whoever runs its organiser.

    Anyone else paying to push a listing up — a rival hall, a prankster, a
    well-meaning fan — puts the editor in the position of approving a
    placement the event's own people never asked for.
    """
    return bool(buyer_relation(event, user))


NOT_YOURS = (
    "Only the person who listed it, or the account that runs its organiser, "
    "can promote an event. If that is you under another account, or you'd "
    "like to sponsor it anyway, contact the editor."
)


def last_upcoming_day(event, now=None):
    """The last day the event is on: the latest *end*, not the latest start.

    Occurrences come in start order, so the last one can be a Saturday
    session inside a Friday-to-Sunday span.
    """
    days = [
        timezone.localtime(o.end or o.start).date()
        for o in event.upcoming_occurrences(now)
    ]
    return max(days, default=None)


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


def _same_event_booked(event, first, last, exclude=None, approved_only=False):
    """One placement per event per day: a second order for the same days
    would charge twice for one card and take a second slot from everyone."""
    orders = SponsorOrder.objects.filter(
        event=event, product=SponsorOrder.Product.PROMOTE_EVENT
    ).overlapping(first, last)
    orders = orders.approved_or_approving() if approved_only else orders.holding_a_slot()
    if exclude is not None:
        orders = orders.exclude(pk=exclude)
    return orders.exists()


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
    if not may_promote(event, buyer):
        raise SponsorError(NOT_YOURS)
    first, last = promote_window(event, starts_on, config, now)
    if _same_event_booked(event, first, last):
        raise SponsorError(
            "This event already has a promotion booked or awaiting review on "
            "some of those days. Try a start date after it ends."
        )
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
            # From Stripe's clock, not ours: an order found by the 25-hour
            # sweep has already used a day of its seven.
            order.mark_authorized(now=info.get("authorized_at"))
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
        order = _withdraw_checkout(order)
        if order.status != Status.AWAITING_REVIEW:
            return order

    def check(o):
        if o.status != Status.AWAITING_REVIEW:
            raise SponsorError("This order has already been decided.")
        _check_stripe(o)

    order = _claim(order.pk, Status.WITHDRAWN, check=check)
    try:
        _release_or_unclaim(order)
    except OutcomeUnknown:
        order.refresh_from_db()
        return order
    except AlreadyCharged as exc:
        raise SponsorError(
            "This order has already been approved and charged, so it can't be "
            "withdrawn. Contact the editor if it shouldn't run."
        ) from exc
    return _finish_quietly(order)


def _withdraw_checkout(order):
    """Close a checkout the buyer called off; returns the order as it now is.

    The order may say "checkout" after the buyer has paid, while the return
    page and the webhook are still on their way. Expiring the session is
    refused then, and the hold is real: the order is brought up to date
    instead, so the caller can withdraw it the ordinary way and release it.
    Marking it abandoned would leave a week's hold on the card with the page
    saying nothing was charged.
    """
    if order.stripe_checkout_session_id:
        try:
            payments.expire_checkout(order.stripe_checkout_session_id)
        except STRIPE_FAILURES:
            try:
                order = sync_checkout(order)
            except STRIPE_FAILURES as exc:
                raise SponsorError(
                    "Stripe can't be reached just now, so nothing has changed. "
                    "Please try again in a few minutes."
                ) from exc
            if order.status == Status.CHECKOUT:
                raise SponsorError(
                    "The payment page is still open or finishing. Close it, "
                    "then try again in a minute."
                )
            return order
    with transaction.atomic():
        order = _locked(order.pk)
        if order.status == Status.CHECKOUT:
            order.status = Status.ABANDONED
            order.decided_at = timezone.now()
            order.save()
    return order


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
    # `of=("self",)` because `event` is nullable, so the join is an outer one,
    # and Postgres refuses FOR UPDATE on the nullable side of an outer join.
    # SQLite ignores FOR UPDATE entirely, which is how this first shipped
    # broken: only Postgres can catch it.
    return (
        SponsorOrder.objects.select_for_update(of=("self",))
        .select_related("event")
        .get(pk=pk)
    )


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
        elif outcome == Status.WITHDRAWN:
            ModerationAction.record(
                order.buyer, "sponsor_withdrawn", event=order.event,
                detail=f"{order}: withdrawn by the buyer; hold released",
            )
            _mail_moderators(order, "withdrawn")
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
    except OutcomeUnknown:
        raise  # Kept claimed: see OutcomeUnknown.
    except AlreadyCharged:
        # The money was taken, so the placement is owed: record it as
        # approved, as a capture made in the dashboard is, and say so.
        order = _unclaim(order)
        try:
            _settle(order, "succeeded")
        except SponsorError:
            pass
        raise
    except SponsorError:
        _unclaim(order)
        raise


def _check_stripe(o):
    """Refuse, before claiming, a decision this site's key cannot carry out."""
    config = SponsorConfig.load()
    if not config.stripe_secret_key:
        raise SponsorError(f"Stripe can't be asked: {_stripe_said(payments.NotConfigured())}")
    if o.stripe_payment_intent_id and o.livemode == config.test_mode:
        paid = "live" if o.livemode else "test"
        stored = "test" if config.test_mode else "live"
        raise SponsorError(
            f"This order was paid in {paid} mode, and the stored Stripe key is a "
            f"{stored} key, which can't see it. Store the {paid} key again to "
            f"decide it, or capture or cancel it in the Stripe dashboard."
        )


def _check_can_charge(moderator):
    if moderator is None or not moderator.has_perm(APPROVE_PERMISSION):
        raise SponsorError(
            "Approving or rejecting a sponsorship needs the “approve and charge "
            "sponsorships” permission, because it moves money. Ask an administrator."
        )


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def approve(order, moderator, note=""):
    """Charge the held card and start the run."""
    _check_can_charge(moderator)

    def check(o):
        if o.status != Status.AWAITING_REVIEW:
            raise SponsorError(f"This order is {o.get_status_display().lower()}, not awaiting review.")
        _check_stripe(o)
        # One approval at a time: without this, two moderators approving at
        # once each count the other as still waiting, and both capture past
        # the daily limit. The lock is held until the claim commits, and a
        # claimed order counts against the limit from then on.
        SponsorConfig.objects.select_for_update().filter(pk=1).first()
        if o.ends_on < timezone.localdate():
            raise SponsorError("Its run has already ended. Reject it to release the hold.")
        # A deleted event leaves `event` empty (SET_NULL), and a charge for
        # a placement with nothing to place is the one thing never owed.
        if not o.event_id or not promotable(o.event):
            raise SponsorError("The event is no longer published. Reject it, or republish the event first.")
        if _same_event_booked(o.event, o.starts_on, o.ends_on, exclude=o.pk, approved_only=True):
            raise SponsorError(
                "This event is already promoted on some of these days by another "
                "order. Reject this one to release its hold."
            )
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
    except STRIPE_FAILURES as exc:
        # An error is not proof that nothing happened: a capture whose reply
        # was lost on the way back has charged the card. Ask before undoing.
        status = _intent_status_or_none(order)
        if status == "succeeded":
            return _finish_quietly(order)
        if status is None and not _definite(exc):
            # No answer either way: leave it claimed for the webhook or
            # housekeeping, which the queue shows as being processed.
            logger.warning("sponsor order %s: capture outcome unknown", order.pk)
            order.refresh_from_db()
            return order
        _unclaim(order)
        raise SponsorError(f"Stripe would not take the payment: {_stripe_said(exc)}") from exc
    if status != "succeeded":
        _unclaim(order)
        raise SponsorError(f"Stripe reports the payment as {status}; not approved.")
    return _finish_quietly(order)


def reject(order, moderator, reason):
    """Release the hold and tell the buyer why."""
    _check_can_charge(moderator)
    if not reason.strip():
        raise SponsorError("Say why, briefly. The buyer gets this with the news.")

    def check(o):
        if o.status != Status.AWAITING_REVIEW:
            raise SponsorError(f"This order is {o.get_status_display().lower()}, not awaiting review.")
        _check_stripe(o)

    order = _claim(order.pk, Status.REJECTED, moderator=moderator, note=reason, check=check)
    try:
        _release_or_unclaim(order)
    except OutcomeUnknown:
        order.refresh_from_db()
        return order
    return _finish_quietly(order)


def _intent_status_or_none(order):
    try:
        return payments.read_intent_status(order.stripe_payment_intent_id)
    except STRIPE_FAILURES:
        logger.exception("could not read payment %s", order.stripe_payment_intent_id)
        return None


def _release(order):
    """Cancel the hold, or raise.

    Stripe refuses to cancel for more than one reason, and only one of them
    means the hold is gone. A payment already *captured* is refused too, and
    so is one this key cannot see (another mode, another account); taking
    either as "released" told a buyer nothing was charged while their card
    had been, or still was. So a refusal is followed by asking what the
    payment actually is.
    """
    intent = order.stripe_payment_intent_id
    if not intent:
        return
    try:
        payments.release(intent)
    except STRIPE_FAILURES as exc:
        # A refusal or a lost reply alike: ask what the payment now is.
        status = None if isinstance(exc, payments.NotConfigured) else _intent_status_or_none(order)
        if status == "canceled":
            logger.info("payment %s was already released", intent)
            return
        if status == "succeeded":
            raise AlreadyCharged(
                "The card had already been charged (in the Stripe dashboard, or "
                "by an earlier approval), so this is now recorded as approved. "
                "Refund it in Stripe if it shouldn't run."
            ) from exc
        if status is None and not _definite(exc):
            raise OutcomeUnknown(
                "Stripe didn't answer, so it isn't known yet whether the hold "
                "was released. It will finish by itself once Stripe replies."
            ) from exc
        raise SponsorError(f"Stripe would not release the hold: {_stripe_said(exc)}") from exc


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
        if order.status in RELEASED:
            _charged_after_release(order)
            return order
        if order.status == Status.PROCESSING and order.pending_status in RELEASED:
            # Heading for a release, but the money was taken first: the
            # placement is owed, and nobody here decided to charge it.
            with transaction.atomic():
                locked = _locked(order.pk)
                if locked.status == Status.PROCESSING:
                    logger.warning("sponsor order %s was charged while being %s", order.pk, locked.pending_status)
                    locked.pending_status = Status.APPROVED
                    locked.decided_by = None
                    locked.decision_note = ""
                    locked.save()
            order.refresh_from_db()
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


def _charged_after_release(order):
    """A charge for an order this site told its buyer was not charged.

    Nothing here can make that right on its own (refund it, or run it), so a
    person is told, once per order.
    """
    detail = f"{order}: charged in Stripe after it was {order.get_status_display().lower()}"
    if ModerationAction.objects.filter(action="sponsor_charged_after_release", detail=detail[:300]).exists():
        return
    logger.error("%s", detail)
    with transaction.atomic():
        ModerationAction.record(None, "sponsor_charged_after_release", event=order.event, detail=detail)
        _mail_moderators(order, "charged_after_release")


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
                if order.status in (Status.APPROVED, Status.STOPPED):
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
        # Abandoned only on Stripe's word. If Stripe can't be read, the
        # checkout may have finished with a hold nobody has recorded, and
        # dropping it from the queue would leave the card held unseen.
        try:
            order = sync_checkout(order)
        except STRIPE_FAILURES:
            logger.exception("could not read checkout for sponsor order %s", order.pk)
            continue
        if order.status != Status.CHECKOUT:
            continue
        if order.stripe_checkout_session_id:
            # Still open past its 24 hours is unexpected; shut it so it
            # cannot complete after the order is closed.
            try:
                payments.expire_checkout(order.stripe_checkout_session_id)
            except stripe.InvalidRequestError:
                pass  # Already closed.
            except STRIPE_FAILURES:
                logger.exception("could not expire checkout for sponsor order %s", order.pk)
                continue
        with transaction.atomic():
            locked = _locked(order.pk)
            if locked.status == Status.CHECKOUT:
                locked.status = Status.ABANDONED
                locked.save(update_fields=["status", "updated_at"])
                done["abandoned"] += 1

    for order in SponsorOrder.objects.filter(
        status=Status.PROCESSING, updated_at__lt=now - STUCK_AFTER
    ):
        try:
            settled = _settle(order, payments.read_intent_status(order.stripe_payment_intent_id))
        except (*STRIPE_FAILURES, SponsorError):
            logger.exception("could not settle sponsor order %s", order.pk)
            continue
        if settled.status != Status.PROCESSING:
            done["settled"] += 1
        else:
            logger.error("sponsor order %s is still processing after settling", order.pk)

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
# When the event goes away
# ---------------------------------------------------------------------------


def live_placements(event):
    """Approved promotions of `event` that are running or still to run."""
    if event is None or event.pk is None:
        return SponsorOrder.objects.none()
    return SponsorOrder.objects.filter(
        event=event, status=Status.APPROVED, ends_on__gte=timezone.localdate()
    )


def stop_placements(event, *, deleted=False):
    """Stop the paid placements of an event that has left the site.

    Called whenever an event is saved in any status but published, and
    before one is deleted, so every way down (Unpublish, Duplicate, the edit
    form's status, the admin) is covered. The placement could not show
    anyway; what this adds is that the order stops saying "Running" and a
    person hears that someone paid for it. Whether that is owed back is the
    editor's call — a listing taken down for breaking the rules is not the
    same as a hall that cancelled — so nothing is refunded here: the audit
    row and the moderators' mail say to refund it in Stripe if it is owed,
    and a refund made there is recorded by the webhook as usual.
    """
    stopped = []
    with transaction.atomic():
        orders = live_placements(event).select_for_update(of=("self",))
        for order in orders:
            order.status = Status.STOPPED
            order.save(update_fields=["status", "updated_at"])
            why = "deleted" if deleted else event.get_status_display().lower()
            ModerationAction.record(
                None, "sponsor_stopped", event=None if deleted else event,
                detail=(
                    f"{order}: refund in Stripe if owed. “{event.title}” was {why}, "
                    f"so its paid placement ({order.starts_on:%-d %b}–{order.ends_on:%-d %b}) stopped."
                ),
            )
            _mail_moderators(order, "stopped")
            stopped.append(order)
    return stopped


# ---------------------------------------------------------------------------
# Mail
# ---------------------------------------------------------------------------


def _mail_buyer(order, kind):
    from .tasks import email_buyer

    transaction.on_commit(lambda: email_buyer.enqueue(order.pk, kind))


def _mail_moderators(order, kind):
    from .tasks import email_moderators

    transaction.on_commit(lambda: email_moderators.enqueue(order.pk, kind))
