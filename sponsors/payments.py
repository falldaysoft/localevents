"""Everything that talks to Stripe, and nothing else.

Kept to one module so the tests can replace it whole, and so the list of
things this site asks of a payment processor is short enough to read: open a
checkout, read back what happened, capture or release a hold, and set up the
webhook. Prices are sent inline (`price_data`) rather than as products made in
the Stripe dashboard, so each instance's prices live in its own admin and two
sites can share one Stripe account without sharing a price list.
"""

from decimal import Decimal

import stripe
from django.conf import settings
from django.urls import reverse

from .models import SponsorConfig

# The events the webhook endpoint is subscribed to. Adding one here does not
# reach an endpoint already made; press "Set up the webhook" again.
WEBHOOK_EVENTS = [
    "checkout.session.completed",
    "checkout.session.expired",
    "payment_intent.amount_capturable_updated",
    "payment_intent.canceled",
    "payment_intent.succeeded",
    "charge.refunded",
]


class NotConfigured(Exception):
    pass


def _client(config=None):
    config = config or SponsorConfig.load()
    key = config.stripe_secret_key
    if not key:
        raise NotConfigured("No Stripe secret key is stored.")
    return stripe.StripeClient(key)


def absolute_url(path):
    return f"{settings.SITE_BASE_URL.rstrip('/')}{path}"


def to_minor(amount):
    """Dollars to cents. Every currency this site would plausibly use has two
    decimal places; a zero-decimal one (JPY) would need this to know so."""
    return int((Decimal(amount) * 100).quantize(Decimal("1")))


def from_minor(value):
    if value is None:
        return None
    return (Decimal(value) / 100).quantize(Decimal("0.01"))


def create_checkout(order, config=None):
    """Open a hosted Checkout page for `order`; returns (session id, url)."""
    config = config or SponsorConfig.load()
    product_data = {"name": order.description[:250]}
    if config.tax_code:
        product_data["tax_code"] = config.tax_code
    detail = reverse("sponsor_order", args=[order.pk])
    params = {
        "mode": "payment",
        "line_items": [{
            "quantity": 1,
            "price_data": {
                "currency": order.currency,
                "unit_amount": to_minor(order.amount),
                "product_data": product_data,
                "tax_behavior": "exclusive",
            },
        }],
        "payment_intent_data": {
            # Held, not charged: a moderator's approval captures it.
            "capture_method": "manual",
            "description": f"{settings.SITE_NAME}: {order.description}"[:1000],
            "metadata": {"sponsor_order": str(order.pk), "site": settings.SITE_BASE_URL},
        },
        "metadata": {"sponsor_order": str(order.pk), "site": settings.SITE_BASE_URL},
        "client_reference_id": str(order.pk),
        "customer_email": order.buyer.email,
        "success_url": absolute_url(reverse("sponsor_order_return", args=[order.pk]))
        + "?session_id={CHECKOUT_SESSION_ID}",
        "cancel_url": absolute_url(detail),
    }
    if config.automatic_tax:
        params["automatic_tax"] = {"enabled": True}
        params["billing_address_collection"] = "required"
    session = _client(config).v1.checkout.sessions.create(params=params)
    return session.id, session.url


def read_checkout(session_id):
    """What Stripe says happened at checkout, as a plain dict.

    `payment_status` is not enough on its own: with manual capture a finished
    checkout is "unpaid" until capture, so the payment intent's own status —
    `requires_capture` once the card is held — is what decides.
    """
    session = _client().v1.checkout.sessions.retrieve(
        session_id, params={"expand": ["payment_intent"]}
    )
    intent = session.payment_intent
    details = session.total_details
    return {
        "session_status": session.status,
        "payment_intent_id": getattr(intent, "id", intent) or "",
        "intent_status": getattr(intent, "status", ""),
        "amount_subtotal": from_minor(session.amount_subtotal),
        "amount_tax": from_minor(getattr(details, "amount_tax", None)),
        "amount_total": from_minor(session.amount_total),
        "livemode": bool(session.livemode),
    }


def read_intent_status(intent_id):
    return _client().v1.payment_intents.retrieve(intent_id).status


def capture(intent_id):
    return _client().v1.payment_intents.capture(intent_id).status


def release(intent_id):
    """Cancel an uncaptured payment: the hold drops off the buyer's card."""
    return _client().v1.payment_intents.cancel(
        intent_id, params={"cancellation_reason": "requested_by_customer"}
    ).status


def expire_checkout(session_id):
    return _client().v1.checkout.sessions.expire(session_id).status


def parse_webhook(payload, signature):
    """Verify and parse a webhook delivery. Raises ValueError or
    `stripe.SignatureVerificationError` on anything that is not genuine."""
    secret = SponsorConfig.load().stripe_webhook_secret
    if not secret:
        raise ValueError("No webhook secret is stored.")
    return stripe.Webhook.construct_event(payload, signature, secret)


def test_connection(config):
    """(ok, sentence) for the admin's Test button."""
    try:
        account = _client(config).v1.accounts.retrieve_current()
    except NotConfigured as exc:
        return False, str(exc)
    except stripe.StripeError as exc:
        return False, f"Stripe refused the key: {exc.user_message or exc}"
    profile = getattr(account, "business_profile", None)
    dashboard = getattr(getattr(account, "settings", None), "dashboard", None)
    name = (
        getattr(profile, "name", None)
        or getattr(dashboard, "display_name", None)
        or account.id
    )
    config.stripe_account_name = str(name)[:200]
    config.save(update_fields=["stripe_account_name"])
    mode = "test" if config.test_mode else "live"
    return True, f"Connected to {name} in {mode} mode."


def set_up_webhook(config):
    """Create this site's webhook endpoint and store its signing secret.

    One button instead of a trip to the dashboard and a secret to copy back:
    the secret is only ever shown once, at creation, which is exactly when
    this code has it. An endpoint this site made earlier is removed first so
    Stripe does not deliver everything twice.
    """
    client = _client(config)
    url = absolute_url(reverse("sponsor_webhook"))
    if config.stripe_webhook_endpoint_id:
        try:
            client.v1.webhook_endpoints.delete(config.stripe_webhook_endpoint_id)
        except stripe.InvalidRequestError:
            pass  # Already gone, deleted in the dashboard.
    endpoint = client.v1.webhook_endpoints.create(params={
        "url": url,
        "enabled_events": WEBHOOK_EVENTS,
        "description": f"{settings.SITE_NAME} sponsorships",
    })
    config.stripe_webhook_endpoint_id = endpoint.id
    config.stripe_webhook_secret = endpoint.secret
    config.save(update_fields=["stripe_webhook_endpoint_id", "stripe_webhook_secret_enc"])
    return url
