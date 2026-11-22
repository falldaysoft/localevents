"""Encryption at rest for the payment keys kept in the database.

The keys live in the database rather than the instance's `.env` so that
nobody has to carry a payment secret through a deploy, a CI variable or a
chat. The cost of that is that every nightly dump now holds them, which is
what this is for: a dump on its own no longer opens the Stripe account.

The key is derived from `SECRET_KEY`, which lives only in the `.env` on the
VM. Rotating `SECRET_KEY` therefore makes the stored values unreadable — they
read back as empty and the admin says so — and the fix is to paste them in
again. That is a deliberate trade: a second secret to manage would be one
more thing to lose.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings

PURPOSE = b"localevents.sponsors.payment-keys"


def _fernet():
    digest = hashlib.sha256(PURPOSE + b"\0" + settings.SECRET_KEY.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value):
    if not value:
        return ""
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token):
    """The stored value, or "" when there is none or it cannot be read."""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return ""


def readable(token):
    """False for a value stored under a different `SECRET_KEY`."""
    return not token or bool(decrypt(token))
