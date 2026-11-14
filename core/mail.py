"""The one email backend, reading the mail server from the admin at send time.

Every message the site sends — allauth's confirmations, moderation decisions,
answers to emailed submissions — goes through here, so a change saved under
Mail configuration applies to the next message without a restart. With no
SMTP host saved there, this hands off to the environment's backend
(`EMAIL_DELIVERY_BACKEND`, console in development), which is exactly how the
site behaved before the setting existed.

The sender is rewritten here too, for the same reason: code all over the site
(and inside allauth) reads `settings.DEFAULT_FROM_EMAIL`, which is fixed at
start-up. A message from that address is re-addressed from the configured
one; a message deliberately sent from anywhere else is left alone.
"""

import logging
from email.utils import formataddr, parseaddr

from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.backends.smtp import EmailBackend as SMTPBackend

logger = logging.getLogger("core.mail")


def with_site_name(address):
    """`noreply@host` becomes `Site Name <noreply@host>`; a named one is kept.

    Without a display name an inbox lists the site's mail as "noreply",
    which is what the weekly email's first real delivery looked like.
    """
    name, addr = parseaddr(address)
    if name or not addr:
        return address
    return formataddr((settings.SITE_NAME, addr))


def _config():
    from .models import MailConfig

    try:
        return MailConfig.load()
    except Exception:
        # Mail is how errors get reported; it must not depend on the database
        # being up to fall back to what the environment already says.
        logger.exception("could not read mail configuration; using environment")
        return None


def delivery_connection(config=None, **kwargs):
    """The connection that will actually carry a message, and a label for it."""
    config = config if config is not None else _config()
    outbound = config.outbound() if config else None
    if outbound:
        kwargs.setdefault("timeout", 30)
        return SMTPBackend(**outbound, **kwargs), f"SMTP {outbound['host']}"
    backend = settings.EMAIL_DELIVERY_BACKEND
    return get_connection(backend, **kwargs), backend.rsplit(".", 1)[-1] + (
        f" ({settings.EMAIL_HOST})" if backend.endswith("smtp.EmailBackend") else ""
    )


class ConfiguredEmailBackend(BaseEmailBackend):
    def __init__(self, fail_silently=False, **kwargs):
        super().__init__(fail_silently=fail_silently)
        self.kwargs = kwargs
        self.config = None
        self.connection = None

    def open(self):
        if self.connection is None:
            self.config = _config()
            self.connection, _ = delivery_connection(
                self.config, fail_silently=self.fail_silently, **self.kwargs
            )
        return self.connection.open()

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def send_messages(self, email_messages):
        if not email_messages:
            return 0
        opened_here = self.connection is None
        self.open()
        try:
            sender = self.config.resolved_from_email() if self.config else None
            for message in email_messages:
                if message.from_email in (
                    settings.DEFAULT_FROM_EMAIL, settings.SERVER_EMAIL
                ):
                    message.from_email = with_site_name(sender or message.from_email)
            return self.connection.send_messages(email_messages)
        finally:
            if opened_here:
                self.close()
