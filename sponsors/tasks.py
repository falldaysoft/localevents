"""Sponsorship mail, sent from the queue.

As with `moderation.tasks`, renaming either function is a two-step deploy:
a queued row naming the old path takes down every background task.
"""

import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from django.db.models import Q
from django.tasks import task
from django.template.loader import render_to_string
from django.urls import reverse

from .models import SponsorOrder

logger = logging.getLogger("sponsors.tasks")

BUYER_SUBJECTS = {
    "received": "We've got your sponsorship request — {site}",
    "approved": "Your sponsorship is approved — {site}",
    "rejected": "About your sponsorship request — {site}",
    "lapsed": "Your sponsorship request has lapsed — {site}",
}

MODERATOR_SUBJECTS = {
    "new": "[{site}] Sponsorship to review: {order}",
    "hold_ending": "[{site}] Decide soon: the card hold on {order} is ending",
}


def _url(path):
    return f"{settings.SITE_BASE_URL.rstrip('/')}{path}"


def _context(order):
    return {
        "order": order,
        "event": order.event,
        "site_name": settings.SITE_NAME,
        "contact_email": settings.CONTACT_EMAIL,
        "order_url": _url(reverse("sponsor_order", args=[order.pk])),
        "review_url": _url(reverse("mod_sponsor", args=[order.pk])),
        "event_url": (
            _url(reverse("event_detail", args=[order.event.slug]))
            if order.event and order.event.slug else ""
        ),
    }


def _order(order_id):
    order = SponsorOrder.objects.select_related("buyer", "event").filter(pk=order_id).first()
    if order is None:
        logger.warning("sponsor order %s is gone", order_id)
    return order


@task()
def email_buyer(order_id, kind):
    order = _order(order_id)
    if order is None or not order.buyer.email:
        return
    send_mail(
        subject=BUYER_SUBJECTS[kind].format(site=settings.SITE_NAME),
        message=render_to_string(f"sponsors/email/{kind}.txt", _context(order)),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[order.buyer.email],
    )


@task()
def email_moderators(order_id, kind):
    """Moderators hear about every order, because each one is on a clock."""
    order = _order(order_id)
    if order is None:
        return
    recipients = list(
        get_user_model().objects.filter(is_active=True)
        .filter(Q(is_superuser=True) | Q(groups__name="Moderators"))
        .exclude(email="")
        .values_list("email", flat=True)
        .distinct()
    )
    if not recipients:
        return
    send_mail(
        subject=MODERATOR_SUBJECTS[kind].format(site=settings.SITE_NAME, order=order.description),
        message=render_to_string(f"sponsors/email/mod_{kind}.txt", _context(order)),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=recipients,
    )
