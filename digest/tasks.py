"""Mail for the weekly email's subscribers.

Renaming anything here is a two-step deploy: a queued row naming the old path
stops the whole worker (see CLAUDE.md).
"""

import logging

from django.conf import settings
from django.core.mail import EmailMessage
from django.tasks import task
from django.template.loader import render_to_string
from django.urls import reverse

from .models import Subscriber

logger = logging.getLogger("digest.tasks")


def absolute_url(path):
    return f"{settings.SITE_BASE_URL.rstrip('/')}{path}"


@task()
def send_confirmation(subscriber_id):
    """Ask the address to confirm, with a link that is good for a week.

    Silent on a row that has gone or is already receiving: the form may have
    been posted twice, and the second confirmation is the one to skip.
    """
    from .services import confirmation_key

    subscriber = Subscriber.objects.filter(pk=subscriber_id).first()
    if subscriber is None or subscriber.is_active:
        return

    context = {
        "site_name": settings.SITE_NAME,
        "contact_email": settings.CONTACT_EMAIL,
        "confirm_url": absolute_url(
            reverse("digest_confirm", args=[confirmation_key(subscriber)])
        ),
    }
    EmailMessage(
        subject=f"Confirm your weekly email from {settings.SITE_NAME}",
        body=render_to_string("digest/email/confirm.txt", context),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[subscriber.email],
        # Replies are for a person, never for the submissions inbox, where
        # triage would read "yes please" as a malformed event.
        reply_to=[settings.CONTACT_EMAIL],
        headers={"Auto-Submitted": "auto-generated"},
    ).send()


@task()
def send_digest_batch(run_id, delivery_ids):
    """Send one batch of a weekly run over one SMTP connection."""
    from .sending import send_batch

    send_batch(run_id, delivery_ids)
