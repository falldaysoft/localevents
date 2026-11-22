"""Paid sponsorships: held at checkout, charged on approval, labelled where shown.

Stripe is never called. `sponsors.payments` is the only module that talks to
it, and these tests replace its functions — except the webhook signature,
which is checked for real against a header built the way Stripe builds one,
because a forged delivery being accepted is the failure that matters most.
"""

import hashlib
import hmac
import json
import time
from datetime import timedelta
from decimal import Decimal

import pytest
import stripe
from django.core import mail
from django.db import OperationalError, connection
from django.urls import reverse
from django.utils import timezone

from digest.content import base_listing, default_filter
from events.models import Event, Occurrence
from sponsors import crypto, payments, services
from sponsors.admin import SponsorConfigForm
from sponsors.models import SponsorConfig, SponsorOrder
from submissions.models import ModerationAction
from web.filters import EventFilter
from web.listing import _listing

Status = SponsorOrder.Status
WEBHOOK_SECRET = "whsec_test_secret"


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def config(db):
    config = SponsorConfig.load()
    config.enabled = True
    config.stripe_secret_key = "sk_test_abcd1234"
    config.stripe_webhook_secret = WEBHOOK_SECRET
    config.save()
    return config


@pytest.fixture
def event(db, buyer):
    # The buyer listed it: only the person who did, or the organiser's owner,
    # may pay to promote a listing.
    event = Event.objects.create(
        title="Harvest Supper", status=Event.Status.PUBLISHED,
        prominence=Event.Prominence.LISTED, submitted_by=buyer,
    )
    Occurrence.objects.create(event=event, start=timezone.now() + timedelta(days=5))
    return event


@pytest.fixture
def buyer(django_user_model):
    return django_user_model.objects.create_user(
        username="organiser", email="organiser@example.com", password="pw-12345678"
    )


@pytest.fixture
def moderator(django_user_model):
    return django_user_model.objects.create_superuser(
        username="editor", email="editor@example.com", password="pw-12345678"
    )


class Calls(list):
    """The calls made, plus `state`: what the fake Stripe will report."""


def outside_transactions():
    """A Stripe call made inside a transaction can charge a card and then be
    rolled back out of the record. Under pytest-django the test itself is the
    outermost atomic block, so any savepoint means code opened one."""
    assert not connection.savepoint_ids, "Stripe was called inside a transaction"


@pytest.fixture
def stripe_calls(monkeypatch):
    """A fake Stripe that records what was asked of it."""
    calls = Calls()
    state = {
        "intent_status": "requires_capture", "session_status": "complete",
        "fail": set(), "errors": {}, "authorized_at": None, "livemode": False,
    }

    def maybe_fail(name):
        outside_transactions()
        # The real client refuses before any network call without a readable key.
        if not SponsorConfig.load().stripe_secret_key:
            raise payments.NotConfigured("No Stripe secret key is stored.")
        if name in state["errors"]:
            raise state["errors"][name]
        if name in state["fail"]:
            raise stripe.APIConnectionError("Stripe is unreachable")

    def create_checkout(order, config=None):
        calls.append(("checkout", order.pk))
        return f"cs_test_{order.pk}", f"https://checkout.stripe.com/c/pay/cs_test_{order.pk}"

    def read_checkout(session_id):
        maybe_fail("read")
        calls.append(("read", session_id))
        return {
            "session_status": state["session_status"],
            "payment_intent_id": f"pi_{session_id}",
            "intent_status": state["intent_status"],
            "amount_subtotal": Decimal("25.00"),
            "amount_tax": Decimal("3.25"),
            "amount_total": Decimal("28.25"),
            "livemode": state["livemode"],
            "authorized_at": state["authorized_at"],
        }

    def capture(intent_id):
        maybe_fail("capture")
        calls.append(("capture", intent_id))
        return "succeeded"

    def release(intent_id):
        maybe_fail("release")
        calls.append(("release", intent_id))
        return "canceled"

    def expire_checkout(session_id):
        maybe_fail("expire")
        calls.append(("expire", session_id))
        return "expired"

    monkeypatch.setattr(payments, "create_checkout", create_checkout)
    monkeypatch.setattr(payments, "read_checkout", read_checkout)
    monkeypatch.setattr(payments, "capture", capture)
    monkeypatch.setattr(payments, "release", release)
    monkeypatch.setattr(payments, "expire_checkout", expire_checkout)
    def read_intent_status(intent_id):
        maybe_fail("intent")
        calls.append(("intent", intent_id))
        return state["intent_status"]

    monkeypatch.setattr(payments, "read_intent_status", read_intent_status)
    calls.state = state
    return calls


@pytest.fixture
def held_order(config, event, buyer, stripe_calls, django_capture_on_commit_callbacks):
    """An order whose card is held, waiting on a moderator."""
    order, _url = services.start_promotion(event, buyer, timezone.localdate())
    with django_capture_on_commit_callbacks(execute=True):
        services.sync_checkout(order)
    order.refresh_from_db()
    mail.outbox.clear()
    return order


def signed(payload, secret=WEBHOOK_SECRET, timestamp=None):
    timestamp = timestamp or int(time.time())
    body = json.dumps(payload)
    signature = hmac.new(
        secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256
    ).hexdigest()
    return body, f"t={timestamp},v1={signature}"


def post_webhook(client, payload, **kwargs):
    body, header = signed(payload, **kwargs)
    return client.post(
        reverse("sponsor_webhook"), data=body, content_type="application/json",
        HTTP_STRIPE_SIGNATURE=header,
    )


def stripe_event(kind, obj):
    return {"id": "evt_1", "object": "event", "type": kind, "data": {"object": obj}}


# --- keys at rest ------------------------------------------------------------


def test_keys_are_encrypted_at_rest_and_read_back(config):
    stored = SponsorConfig.objects.get(pk=1)
    assert "sk_test_abcd1234" not in stored.stripe_secret_key_enc
    assert stored.stripe_secret_key == "sk_test_abcd1234"
    assert stored.key_hint == "test key ending …1234"


def test_a_changed_secret_key_makes_stored_keys_unreadable_not_an_error(config, settings):
    settings.SECRET_KEY = "a-different-secret"
    stored = SponsorConfig.objects.get(pk=1)
    assert stored.stripe_secret_key == ""
    assert not stored.stripe_ready
    assert "unreadable" in stored.key_hint
    assert not crypto.readable(stored.stripe_secret_key_enc)


def test_admin_form_keeps_the_key_when_left_blank_and_never_echoes_it(config):
    form = SponsorConfigForm(
        data={"currency": "CAD", "promote_price": "30.00", "promote_max_days": 14,
              "promote_daily_slots": 2, "enabled": "on"},
        instance=config,
    )
    assert form.is_valid(), form.errors
    form.save()
    config.refresh_from_db()
    assert config.stripe_secret_key == "sk_test_abcd1234"
    assert config.currency == "cad"
    assert "sk_test_abcd1234" not in str(SponsorConfigForm(instance=config))


def test_admin_form_refuses_a_publishable_key(config):
    form = SponsorConfigForm(
        data={"currency": "cad", "promote_price": "25", "promote_max_days": 14,
              "promote_daily_slots": 2, "stripe_secret_key": "pk_test_nope"},
        instance=config,
    )
    assert not form.is_valid()
    assert "stripe_secret_key" in form.errors


def test_admin_page_is_for_superusers_only(client, config, django_user_model):
    from django.contrib.auth.models import Group

    staff = django_user_model.objects.create_user(
        username="mod", email="mod@example.com", password="pw-12345678", is_staff=True
    )
    staff.groups.add(Group.objects.get_or_create(name="Moderators")[0])
    client.force_login(staff)
    url = reverse("admin:sponsors_sponsorconfig_change", args=[1])
    assert client.get(url).status_code in (302, 403)


# --- buying ------------------------------------------------------------------


def test_nothing_is_for_sale_until_switched_on_and_configured(client, event, buyer, db):
    client.force_login(buyer)
    assert client.get(reverse("sponsor_promote", args=[event.slug])).status_code == 404
    assert client.get(reverse("sponsor_info")).status_code == 404
    page = client.get(reverse("event_detail", args=[event.slug]))
    assert b"Promote this event" not in page.content


def test_the_event_page_offers_promotion_once_on_sale(client, config, event):
    page = client.get(reverse("event_detail", args=[event.slug]))
    assert b"Promote this event" in page.content


def test_buying_sends_the_buyer_to_stripe_checkout(client, config, event, buyer, stripe_calls):
    client.force_login(buyer)
    response = client.post(
        reverse("sponsor_promote", args=[event.slug]),
        {"starts_on": timezone.localdate().isoformat(), "note": "Our first year!", "agree": "on"},
    )
    assert response.status_code == 303
    assert response["Location"].startswith("https://checkout.stripe.com/")
    order = SponsorOrder.objects.get()
    assert order.status == Status.CHECKOUT
    assert order.buyer == buyer and order.event == event
    assert order.amount == Decimal("25.00") and order.currency == "cad"
    assert order.buyer_note == "Our first year!"
    assert order.stripe_checkout_session_id == f"cs_test_{order.pk}"


def test_checkout_asks_stripe_to_hold_not_charge(config, event, buyer, monkeypatch):
    seen = {}

    class Sessions:
        def create(self, params):
            seen.update(params)

            class Session:
                id, url = "cs_1", "https://checkout.stripe.com/x"
            return Session()

    class Client:
        class v1:
            class checkout:
                sessions = Sessions()

    monkeypatch.setattr(payments, "_client", lambda config=None: Client())
    config.automatic_tax = True
    config.save()
    order = SponsorOrder.objects.create(
        buyer=buyer, event=event, starts_on=timezone.localdate(),
        ends_on=timezone.localdate(), currency="cad", amount=Decimal("25.00"),
    )
    payments.create_checkout(order, config)
    assert seen["payment_intent_data"]["capture_method"] == "manual"
    # Stripe refuses payment_method_types on a Checkout Session now; sending
    # it broke every checkout against the real API while these fakes passed.
    assert "payment_method_types" not in seen
    assert set(seen["excluded_payment_method_types"]) == {"klarna", "affirm", "afterpay_clearpay"}
    assert seen["line_items"][0]["price_data"]["unit_amount"] == 2500
    assert seen["line_items"][0]["price_data"]["currency"] == "cad"
    assert seen["automatic_tax"] == {"enabled": True}
    assert seen["metadata"]["sponsor_order"] == str(order.pk)
    assert seen["customer_email"] == "organiser@example.com"
    assert "{CHECKOUT_SESSION_ID}" in seen["success_url"]


def test_the_run_ends_with_the_event_and_never_exceeds_the_limit(config, event):
    today = timezone.localdate()
    first, last = services.promote_window(event, today, config)
    assert first == today
    assert last == timezone.localtime(event.occurrences.get().start).date()

    Occurrence.objects.create(event=event, start=timezone.now() + timedelta(days=60))
    first, last = services.promote_window(event, today, config)
    assert (last - first).days + 1 == config.promote_max_days


def test_a_start_in_the_past_or_after_the_event_is_refused(config, event):
    today = timezone.localdate()
    with pytest.raises(services.SponsorError):
        services.promote_window(event, today - timedelta(days=1), config)
    with pytest.raises(services.SponsorError):
        services.promote_window(event, today + timedelta(days=30), config)


def test_an_unpublished_event_cannot_be_promoted(config, event, buyer, stripe_calls):
    event.status = Event.Status.PENDING
    event.save()
    with pytest.raises(services.SponsorError):
        services.start_promotion(event, buyer, timezone.localdate())
    assert not stripe_calls


def test_full_days_are_refused_before_anyone_pays(config, event, buyer, stripe_calls):
    config.promote_daily_slots = 1
    config.save()
    today = timezone.localdate()
    SponsorOrder.objects.create(
        buyer=buyer, event=event, starts_on=today, ends_on=today + timedelta(days=3),
        currency="cad", amount=Decimal("25"), status=Status.AWAITING_REVIEW,
    )
    with pytest.raises(services.SponsorError, match="taken"):
        services.start_promotion(event, buyer, today)
    # Abandoned and rejected orders hold nothing.
    SponsorOrder.objects.update(status=Status.REJECTED)
    services.start_promotion(event, buyer, today)


def test_coming_back_from_checkout_records_the_hold_and_tells_everyone(
    client, config, event, buyer, moderator, stripe_calls, django_capture_on_commit_callbacks
):
    order, _url = services.start_promotion(event, buyer, timezone.localdate())
    client.force_login(buyer)
    with django_capture_on_commit_callbacks(execute=True):
        response = client.get(reverse("sponsor_order_return", args=[order.pk]))
    assert response.status_code == 302
    order.refresh_from_db()
    assert order.status == Status.AWAITING_REVIEW
    assert order.stripe_payment_intent_id == f"pi_cs_test_{order.pk}"
    assert order.total == Decimal("28.25")
    assert order.hold_expires_at > timezone.now() + timedelta(days=5)
    recipients = sorted(m.to[0] for m in mail.outbox)
    assert recipients == ["editor@example.com", "organiser@example.com"]
    buyer_mail = next(m for m in mail.outbox if m.to == ["organiser@example.com"])
    assert "not charged" in buyer_mail.body

    # The webhook arriving afterwards changes nothing and mails nobody again.
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        services.sync_checkout(order)
    assert mail.outbox == []


def test_someone_else_cannot_see_your_order(client, held_order, django_user_model):
    other = django_user_model.objects.create_user(
        username="nosy", email="nosy@example.com", password="pw-12345678"
    )
    client.force_login(other)
    assert client.get(reverse("sponsor_order", args=[held_order.pk])).status_code == 404


def test_the_buyer_can_withdraw_and_the_hold_is_released(
    client, held_order, buyer, moderator, stripe_calls, django_capture_on_commit_callbacks
):
    client.force_login(buyer)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("sponsor_order_withdraw", args=[held_order.pk]))
    held_order.refresh_from_db()
    assert held_order.status == Status.WITHDRAWN
    assert ("release", held_order.stripe_payment_intent_id) in stripe_calls
    assert ModerationAction.objects.get(action="sponsor_withdrawn").actor == buyer
    assert [m.to for m in mail.outbox] == [["editor@example.com"]]
    assert "Withdrawn by the buyer" in mail.outbox[0].subject


# --- the webhook -------------------------------------------------------------


def test_a_forged_webhook_is_refused(client, config, event, buyer, stripe_calls):
    order, _url = services.start_promotion(event, buyer, timezone.localdate())
    payload = stripe_event("checkout.session.completed", {
        "id": order.stripe_checkout_session_id, "object": "checkout.session",
        "metadata": {"sponsor_order": str(order.pk)},
    })
    response = post_webhook(client, payload, secret="whsec_somebody_else")
    assert response.status_code == 400
    order.refresh_from_db()
    assert order.status == Status.CHECKOUT


def test_a_genuine_webhook_completes_the_checkout(
    client, config, event, buyer, stripe_calls, django_capture_on_commit_callbacks
):
    order, _url = services.start_promotion(event, buyer, timezone.localdate())
    payload = stripe_event("checkout.session.completed", {
        "id": order.stripe_checkout_session_id, "object": "checkout.session",
        "metadata": {"sponsor_order": str(order.pk)},
    })
    with django_capture_on_commit_callbacks(execute=True):
        response = post_webhook(client, payload)
    assert response.status_code == 200
    order.refresh_from_db()
    assert order.status == Status.AWAITING_REVIEW


def test_an_abandoned_checkout_is_closed(client, config, event, buyer, stripe_calls):
    order, _url = services.start_promotion(event, buyer, timezone.localdate())
    stripe_calls.state.update(intent_status="", session_status="expired")
    payload = stripe_event("checkout.session.expired", {
        "id": order.stripe_checkout_session_id, "object": "checkout.session",
        "metadata": {"sponsor_order": str(order.pk)},
    })
    post_webhook(client, payload)
    order.refresh_from_db()
    assert order.status == Status.ABANDONED


def test_a_hold_cancelled_at_stripe_lapses_the_order(
    client, held_order, django_capture_on_commit_callbacks
):
    payload = stripe_event("payment_intent.canceled", {
        "id": held_order.stripe_payment_intent_id, "object": "payment_intent",
        "metadata": {},
    })
    with django_capture_on_commit_callbacks(execute=True):
        post_webhook(client, payload)
    held_order.refresh_from_db()
    assert held_order.status == Status.LAPSED
    assert "wasn't reviewed in time" in mail.outbox[0].body


# --- deciding ----------------------------------------------------------------


def test_moderators_only(client, held_order, buyer):
    client.force_login(buyer)
    assert client.get(reverse("mod_sponsors")).status_code == 403
    assert client.post(reverse("mod_sponsor_approve", args=[held_order.pk])).status_code == 403
    held_order.refresh_from_db()
    assert held_order.status == Status.AWAITING_REVIEW


def test_the_queue_lists_what_is_waiting_with_its_deadline(client, held_order, moderator):
    client.force_login(moderator)
    page = client.get(reverse("mod_sponsors"))
    assert b"Sponsored listing: Harvest Supper" in page.content
    assert b"Hold runs out in" in page.content
    detail = client.get(reverse("mod_sponsor", args=[held_order.pk]))
    assert b"Approve and charge" in detail.content


def test_approval_charges_the_card_records_it_and_tells_the_buyer(
    client, held_order, moderator, stripe_calls, django_capture_on_commit_callbacks
):
    client.force_login(moderator)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("mod_sponsor_approve", args=[held_order.pk]), {"note": "Enjoy!"})
    held_order.refresh_from_db()
    assert held_order.status == Status.APPROVED
    assert held_order.decided_by == moderator
    assert ("capture", held_order.stripe_payment_intent_id) in stripe_calls
    assert ModerationAction.objects.filter(action="sponsor_approved", event=held_order.event).exists()
    assert mail.outbox[0].to == ["organiser@example.com"]
    assert "Enjoy!" in mail.outbox[0].body


def test_rejection_releases_the_hold_and_needs_a_reason(
    client, held_order, moderator, stripe_calls, django_capture_on_commit_callbacks
):
    with pytest.raises(services.SponsorError):
        services.reject(held_order, moderator, "  ")
    client.force_login(moderator)
    with django_capture_on_commit_callbacks(execute=True):
        client.post(reverse("mod_sponsor_reject", args=[held_order.pk]), {"note": "Not a fit."})
    held_order.refresh_from_db()
    assert held_order.status == Status.REJECTED
    assert ("release", held_order.stripe_payment_intent_id) in stripe_calls
    assert not any(call[0] == "capture" for call in stripe_calls)
    assert "Not a fit." in mail.outbox[0].body
    assert "Nothing was charged" in mail.outbox[0].body


def test_an_event_taken_down_since_cannot_be_approved(held_order, moderator, stripe_calls):
    Event.objects.filter(pk=held_order.event_id).update(status=Event.Status.UNPUBLISHED)
    with pytest.raises(services.SponsorError, match="no longer published"):
        services.approve(held_order, moderator)
    assert not any(call[0] == "capture" for call in stripe_calls)


def test_approval_respects_the_daily_limit(held_order, moderator, buyer, stripe_calls):
    config = SponsorConfig.load()
    config.promote_daily_slots = 1
    config.save()
    SponsorOrder.objects.create(
        buyer=buyer, event=held_order.event, starts_on=held_order.starts_on,
        ends_on=held_order.ends_on, currency="cad", amount=Decimal("25"),
        status=Status.APPROVED,
    )
    with pytest.raises(services.SponsorError, match="daily limit"):
        services.approve(held_order, moderator)


# --- where it shows ----------------------------------------------------------


@pytest.fixture
def running(held_order, moderator, stripe_calls):
    services.approve(held_order, moderator)
    held_order.refresh_from_db()
    return held_order


def test_a_running_promotion_is_pinned_and_labelled_on_the_front_page(client, running):
    listing = _listing(EventFilter(), timezone.now())
    assert [e.title for e in listing["featured"]] == ["Harvest Supper"]
    assert listing["featured"][0].is_sponsored
    assert all(e.pk != running.event_id for day in listing["days"] for e in day["events"])
    page = client.get(reverse("index"))
    assert b"Sponsored" in page.content


def test_the_editors_own_picks_come_first(running):
    pick = Event.objects.create(
        title="Editor's Pick", status=Event.Status.PUBLISHED,
        prominence=Event.Prominence.FEATURED,
    )
    Occurrence.objects.create(event=pick, start=timezone.now() + timedelta(days=9))
    featured = _listing(EventFilter(), timezone.now())["featured"]
    assert [e.title for e in featured] == ["Editor's Pick", "Harvest Supper"]
    assert not getattr(featured[0], "is_sponsored", False)


def test_a_promotion_respects_the_readers_filters(running):
    other_filter = EventFilter(venue="somewhere-else")
    assert _listing(other_filter, timezone.now())["featured"] == []


def test_a_promotion_appears_in_the_weekly_email_marked_sponsored(running):
    from django.template.loader import render_to_string

    base = base_listing(default_filter(), timezone.now())
    assert [e.title for e in base["featured"]] == ["Harvest Supper"]
    text = render_to_string("digest/email/digest.txt", {
        **base, "favorites": [], "marked": set(), "base_url": "https://example.org",
        "site_name": "Site",
    })
    assert "Harvest Supper [Sponsored]" in text


def test_a_promotion_outside_its_dates_does_not_show(running):
    SponsorOrder.objects.filter(pk=running.pk).update(
        starts_on=timezone.localdate() + timedelta(days=2)
    )
    assert _listing(EventFilter(), timezone.now())["featured"] == []


def test_a_refund_from_the_dashboard_ends_the_placement(client, running):
    payload = stripe_event("charge.refunded", {
        "id": "ch_1", "object": "charge", "refunded": True,
        "payment_intent": running.stripe_payment_intent_id,
    })
    post_webhook(client, payload)
    running.refresh_from_db()
    assert running.status == Status.REFUNDED
    assert _listing(EventFilter(), timezone.now())["featured"] == []


# --- housekeeping ------------------------------------------------------------


def test_housekeeping_warns_before_a_hold_runs_out_then_releases_it(
    held_order, moderator, stripe_calls, django_capture_on_commit_callbacks
):
    soon = held_order.hold_expires_at - timedelta(days=1)
    with django_capture_on_commit_callbacks(execute=True):
        done = services.housekeeping(now=soon)
    assert done["warned"] == 1
    assert mail.outbox[0].to == ["editor@example.com"]
    assert "Decide soon" in mail.outbox[0].subject

    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        assert services.housekeeping(now=soon)["warned"] == 0  # Once only.
        done = services.housekeeping(now=held_order.hold_expires_at + timedelta(minutes=1))
    assert done["lapsed"] == 1
    held_order.refresh_from_db()
    assert held_order.status == Status.LAPSED
    assert ("release", held_order.stripe_payment_intent_id) in stripe_calls
    assert mail.outbox[0].to == ["organiser@example.com"]


def test_housekeeping_closes_checkouts_nobody_finished(config, event, buyer, stripe_calls):
    order, _url = services.start_promotion(event, buyer, timezone.localdate())
    stripe_calls.state.update(intent_status="", session_status="open")
    SponsorOrder.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(hours=26))
    assert services.housekeeping()["abandoned"] == 1
    order.refresh_from_db()
    assert order.status == Status.ABANDONED


def test_the_moderator_tabs_count_orders_awaiting_review(held_order, moderator):
    from moderation.services import queue_counts

    assert queue_counts(moderator)["sponsors"] == 1


# --- a decision interrupted ----------------------------------------------------
#
# Found in the first local test: SQLite reported "database is locked" after
# Stripe had captured the payment, the decision rolled back, and the webhook
# later marked the order approved with nobody's name on it and no mail.


def test_a_record_that_fails_after_the_charge_is_finished_by_the_webhook(
    client, held_order, moderator, stripe_calls, monkeypatch,
    django_capture_on_commit_callbacks,
):
    real_finish = services.finish

    def locked(order):
        raise OperationalError("database is locked")

    monkeypatch.setattr(services, "finish", locked)
    order = services.approve(held_order, moderator, note="Enjoy!")
    assert order.status == Status.PROCESSING
    assert order.pending_status == Status.APPROVED
    assert order.decided_by == moderator
    assert ("capture", held_order.stripe_payment_intent_id) in stripe_calls

    monkeypatch.setattr(services, "finish", real_finish)
    payload = stripe_event("payment_intent.succeeded", {
        "id": held_order.stripe_payment_intent_id, "object": "payment_intent", "metadata": {},
    })
    with django_capture_on_commit_callbacks(execute=True):
        assert post_webhook(client, payload).status_code == 200
    order.refresh_from_db()
    assert order.status == Status.APPROVED
    assert order.decided_by == moderator
    action = ModerationAction.objects.get(action="sponsor_approved")
    assert action.actor == moderator
    assert mail.outbox[0].to == ["organiser@example.com"]
    assert "Enjoy!" in mail.outbox[0].body


def test_the_moderator_is_told_the_charge_went_through(client, held_order, moderator, stripe_calls, monkeypatch):
    monkeypatch.setattr(services, "finish", lambda order: (_ for _ in ()).throw(OperationalError("locked")))
    client.force_login(moderator)
    response = client.post(reverse("mod_sponsor_approve", args=[held_order.pk]), follow=True)
    assert b"no need to press the button again" in response.content
    assert b"Being processed" in response.content


def test_a_refused_capture_leaves_the_order_as_it_was(held_order, moderator, stripe_calls):
    stripe_calls.state["fail"].add("capture")
    with pytest.raises(services.SponsorError, match="would not take the payment"):
        services.approve(held_order, moderator, note="Enjoy!")
    held_order.refresh_from_db()
    assert held_order.status == Status.AWAITING_REVIEW
    assert held_order.decided_by is None and held_order.decision_note == ""
    assert not ModerationAction.objects.exists()


def test_a_refused_release_leaves_the_order_as_it_was(held_order, moderator, stripe_calls):
    stripe_calls.state["fail"].add("release")
    with pytest.raises(services.SponsorError, match="would not release"):
        services.reject(held_order, moderator, "Not a fit.")
    held_order.refresh_from_db()
    assert held_order.status == Status.AWAITING_REVIEW


def test_a_capture_made_in_the_stripe_dashboard_is_recorded_and_announced(
    client, held_order, django_capture_on_commit_callbacks
):
    payload = stripe_event("payment_intent.succeeded", {
        "id": held_order.stripe_payment_intent_id, "object": "payment_intent", "metadata": {},
    })
    with django_capture_on_commit_callbacks(execute=True):
        post_webhook(client, payload)
    held_order.refresh_from_db()
    assert held_order.status == Status.APPROVED
    action = ModerationAction.objects.get(action="sponsor_approved")
    assert action.actor is None
    assert "outside the queue" in action.detail
    assert mail.outbox[0].to == ["organiser@example.com"]


def test_housekeeping_finishes_a_decision_stripe_completed(
    held_order, moderator, stripe_calls, monkeypatch
):
    monkeypatch.setattr(services, "finish", lambda order: (_ for _ in ()).throw(OperationalError("locked")))
    services.approve(held_order, moderator)
    monkeypatch.undo()  # Restores finish, and the fake Stripe with it.
    monkeypatch.setattr(payments, "read_intent_status", lambda intent_id: "succeeded")
    later = timezone.now() + services.STUCK_AFTER + timedelta(minutes=1)
    assert services.housekeeping(now=later)["settled"] == 1
    held_order.refresh_from_db()
    assert held_order.status == Status.APPROVED
    assert ModerationAction.objects.get(action="sponsor_approved").actor == moderator


def test_housekeeping_undoes_a_claim_whose_capture_never_happened(held_order, moderator):
    SponsorOrder.objects.filter(pk=held_order.pk).update(
        status=Status.PROCESSING, pending_status=Status.APPROVED, decided_by=moderator,
    )
    from unittest import mock

    with mock.patch.object(payments, "read_intent_status", return_value="requires_capture"):
        services.housekeeping(now=timezone.now() + services.STUCK_AFTER + timedelta(minutes=1))
    held_order.refresh_from_db()
    assert held_order.status == Status.AWAITING_REVIEW
    assert held_order.decided_by is None


def test_a_refund_is_written_to_the_log(client, running):
    payload = stripe_event("charge.refunded", {
        "id": "ch_1", "object": "charge", "refunded": True,
        "payment_intent": running.stripe_payment_intent_id,
    })
    post_webhook(client, payload)
    assert ModerationAction.objects.filter(action="sponsor_refunded").exists()


# --- review findings, PR #3 --------------------------------------------------
# Each of these failed before its fix. The numbers are the review's.


def _captured_already():
    return stripe.InvalidRequestError(
        "You cannot cancel this PaymentIntent because it has a status of succeeded.",
        param=None, code="payment_intent_unexpected_state",
    )


def test_1_a_reject_after_the_card_was_charged_is_refused_and_recorded_as_charged(
    held_order, moderator, stripe_calls, django_capture_on_commit_callbacks
):
    # Captured in the dashboard, or by an approve whose reply was lost.
    stripe_calls.state["errors"]["release"] = _captured_already()
    stripe_calls.state["intent_status"] = "succeeded"
    with django_capture_on_commit_callbacks(execute=True):
        with pytest.raises(services.SponsorError, match="already been charged"):
            services.reject(held_order, moderator, "Not a fit")
    held_order.refresh_from_db()
    assert held_order.status == Status.APPROVED
    assert not ModerationAction.objects.filter(action="sponsor_rejected").exists()
    assert not any("About your sponsorship request" in m.subject for m in mail.outbox)


def test_1_an_approve_whose_reply_was_lost_is_recorded_as_charged(
    held_order, moderator, stripe_calls
):
    # The capture went through and the response timed out on the way back.
    stripe_calls.state["fail"].add("capture")
    stripe_calls.state["intent_status"] = "succeeded"
    order = services.approve(held_order, moderator)
    assert order.status == Status.APPROVED
    assert ModerationAction.objects.get(action="sponsor_approved").actor == moderator


def test_1_a_payment_stripe_does_not_know_is_not_taken_as_released(
    held_order, moderator, stripe_calls
):
    # "No such payment_intent" — another mode's key, or another account's.
    missing = stripe.InvalidRequestError("No such payment_intent", param="intent")
    stripe_calls.state["errors"].update(release=missing, intent=missing)
    with pytest.raises(services.SponsorError):
        services.reject(held_order, moderator, "Not a fit")
    held_order.refresh_from_db()
    assert held_order.status == Status.AWAITING_REVIEW


def test_1_a_hold_already_cancelled_still_counts_as_released(
    held_order, moderator, stripe_calls
):
    stripe_calls.state["errors"]["release"] = stripe.InvalidRequestError(
        "status of canceled", param=None, code="payment_intent_unexpected_state"
    )
    stripe_calls.state["intent_status"] = "canceled"
    assert services.reject(held_order, moderator, "Not a fit").status == Status.REJECTED


def test_1_a_charge_arriving_for_a_released_order_raises_the_alarm(
    client, held_order, moderator, stripe_calls, django_capture_on_commit_callbacks
):
    services.reject(held_order, moderator, "Not a fit")
    mail.outbox.clear()
    payload = stripe_event("payment_intent.succeeded", {
        "id": held_order.stripe_payment_intent_id, "object": "payment_intent",
    })
    with django_capture_on_commit_callbacks(execute=True):
        assert post_webhook(client, payload).status_code == 200
    assert ModerationAction.objects.filter(action="sponsor_charged_after_release").exists()
    assert any("Charged after" in m.subject for m in mail.outbox)


def test_2_withdrawing_at_checkout_after_paying_releases_the_hold(
    config, event, buyer, stripe_calls
):
    order, _url = services.start_promotion(event, buyer, timezone.localdate())
    # Paid, but neither the return page nor the webhook has landed yet; a
    # completed session can no longer be expired.
    stripe_calls.state["errors"]["expire"] = stripe.InvalidRequestError(
        "Only open sessions can be expired", param=None
    )
    order = services.withdraw(order, buyer)
    assert order.status == Status.WITHDRAWN
    assert ("release", order.stripe_payment_intent_id) in stripe_calls


def test_2_withdrawing_at_checkout_while_stripe_is_down_changes_nothing(
    config, event, buyer, stripe_calls
):
    order, _url = services.start_promotion(event, buyer, timezone.localdate())
    stripe_calls.state["fail"].update({"expire", "read"})
    with pytest.raises(services.SponsorError):
        services.withdraw(order, buyer)
    order.refresh_from_db()
    assert order.status == Status.CHECKOUT


def test_3_housekeeping_leaves_a_checkout_alone_when_stripe_is_unreachable(
    config, event, buyer, stripe_calls
):
    order, _url = services.start_promotion(event, buyer, timezone.localdate())
    SponsorOrder.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(hours=26))
    stripe_calls.state["fail"].add("read")
    assert services.housekeeping()["abandoned"] == 0
    order.refresh_from_db()
    assert order.status == Status.CHECKOUT


@pytest.mark.django_db(transaction=True)
def test_4_two_approvals_at_once_cannot_exceed_the_daily_limit(
    config, event, buyer, moderator, stripe_calls, monkeypatch
):
    if connection.vendor != "postgresql":
        pytest.skip("SQLite takes no row locks; the race needs Postgres to show.")
    import threading

    config.promote_daily_slots = 1
    config.save()
    today = timezone.localdate()
    orders = [
        SponsorOrder.objects.create(
            buyer=buyer, event=event, starts_on=today, ends_on=today,
            currency="cad", amount=Decimal("25"), status=Status.AWAITING_REVIEW,
            stripe_payment_intent_id=f"pi_race_{n}",
            hold_expires_at=timezone.now() + timedelta(days=5),
        )
        for n in range(2)
    ]
    # Hold both approvals between their slot count and their claim, so that
    # without a lock each counts before the other has claimed.
    barrier = threading.Barrier(2)
    real_full_days = services.full_days

    def full_days(*args, **kwargs):
        result = real_full_days(*args, **kwargs)
        try:
            barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return result

    monkeypatch.setattr(services, "full_days", full_days)
    outcomes = []

    def run(order):
        from django.db import connection as thread_connection

        try:
            services.approve(order, moderator)
            outcomes.append("approved")
        except services.SponsorError:
            outcomes.append("refused")
        finally:
            thread_connection.close()

    threads = [threading.Thread(target=run, args=(order,)) for order in orders]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert sorted(outcomes) == ["approved", "refused"]
    assert SponsorOrder.objects.filter(status=Status.APPROVED).count() == 1


def test_5_the_hold_deadline_counts_from_the_authorisation(config, event, buyer, stripe_calls):
    order, _url = services.start_promotion(event, buyer, timezone.localdate())
    authorized = timezone.now() - timedelta(hours=26)  # Found by the sweep, a day late.
    stripe_calls.state["authorized_at"] = authorized
    order = services.sync_checkout(order)
    assert order.authorized_at == authorized
    assert order.hold_expires_at == authorized + timedelta(days=6)


@pytest.mark.parametrize("decide", ["approve", "reject"])
def test_6_an_unreadable_key_refuses_the_decision_and_leaves_the_order_waiting(
    held_order, moderator, stripe_calls, settings, decide
):
    settings.SECRET_KEY = "rotated-without-pasting-the-keys-again"
    with pytest.raises(services.SponsorError, match="key"):
        if decide == "approve":
            services.approve(held_order, moderator)
        else:
            services.reject(held_order, moderator, "Not a fit")
    held_order.refresh_from_db()
    assert held_order.status == Status.AWAITING_REVIEW


def test_7_a_new_key_forgets_the_old_webhook(config):
    config.stripe_webhook_endpoint_id = "we_old"
    config.save()
    form = SponsorConfigForm(
        data={"currency": "cad", "promote_price": "25.00", "promote_max_days": 14,
              "promote_daily_slots": 2, "enabled": "on",
              "stripe_secret_key": "sk_live_newkey9999"},
        instance=config,
    )
    assert form.is_valid(), form.errors
    form.save()
    config = SponsorConfig.objects.get(pk=1)
    assert config.stripe_secret_key == "sk_live_newkey9999"
    assert config.stripe_webhook_secret == ""
    assert config.stripe_webhook_endpoint_id == ""
    assert not config.stripe_ready


def test_7_saving_the_same_key_again_keeps_the_webhook(config):
    form = SponsorConfigForm(
        data={"currency": "cad", "promote_price": "25.00", "promote_max_days": 14,
              "promote_daily_slots": 2, "enabled": "on",
              "stripe_secret_key": "sk_test_abcd1234"},
        instance=config,
    )
    assert form.is_valid(), form.errors
    form.save()
    assert SponsorConfig.objects.get(pk=1).stripe_webhook_secret == WEBHOOK_SECRET


@pytest.mark.parametrize("decide", ["approve", "reject"])
def test_7_an_order_from_the_other_mode_is_refused(held_order, moderator, stripe_calls, decide):
    SponsorOrder.objects.filter(pk=held_order.pk).update(livemode=True)
    held_order.refresh_from_db()
    stripe_calls.clear()
    with pytest.raises(services.SponsorError, match="live mode"):
        if decide == "approve":
            services.approve(held_order, moderator)
        else:
            services.reject(held_order, moderator, "Not a fit")
    assert not any(call[0] in ("capture", "release") for call in stripe_calls)


def test_8_only_the_lister_or_the_organiser_can_promote(
    client, config, event, stripe_calls, django_user_model
):
    from events.models import Organizer

    stranger = django_user_model.objects.create_user(
        username="rival", email="rival@example.com", password="pw-12345678"
    )
    with pytest.raises(services.SponsorError, match="listed it"):
        services.start_promotion(event, stranger, timezone.localdate())
    client.force_login(stranger)
    assert b"Promote this event" not in client.get(reverse("event_detail", args=[event.slug])).content
    page = client.get(reverse("sponsor_promote", args=[event.slug]))
    assert b"only the person who listed it" in page.content.lower()

    event.organizer = Organizer.objects.create(name="Harvest Committee", owner=stranger)
    event.save()
    order, _url = services.start_promotion(event, stranger, timezone.localdate())
    assert order.status == Status.CHECKOUT


def test_9_approving_a_charge_needs_its_own_permission(
    client, held_order, stripe_calls, django_user_model
):
    from django.contrib.auth.models import Group, Permission

    helper = django_user_model.objects.create_user(
        username="helper", email="helper@example.com", password="pw-12345678"
    )
    helper.groups.add(Group.objects.get_or_create(name="Moderators")[0])
    with pytest.raises(services.SponsorError, match="permission"):
        services.approve(held_order, helper)
    with pytest.raises(services.SponsorError, match="permission"):
        services.reject(held_order, helper, "Not a fit")
    client.force_login(helper)
    page = client.get(reverse("mod_sponsor", args=[held_order.pk]))
    assert page.status_code == 200
    assert b"Approve and charge" not in page.content

    helper.user_permissions.add(Permission.objects.get(codename="approve_sponsororder"))
    helper = django_user_model.objects.get(pk=helper.pk)  # Drop the permission cache.
    assert services.approve(held_order, helper).status == Status.APPROVED
