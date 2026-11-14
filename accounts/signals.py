"""What signing in does to the marks a device made before it had an account."""

from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver

from events import interest
from events.services import merge_anonymous_interest


@receiver(user_logged_in)
def claim_anonymous_interest(sender, request, user, **kwargs):
    """Fold this device's anonymous "Interested" marks into the account.

    Django's own signal rather than allauth's, so it covers every way in —
    password, passkey, the confirmation link — because they all end in
    `django.contrib.auth.login`. The cookie is left in place: its marks now
    belong to the account, so after signing out the device starts clean, which
    is what sharing a laptop needs.
    """
    if request is None:
        return
    merge_anonymous_interest(user, interest.fingerprint(request))


@receiver(user_logged_in)
def claim_digest_subscription(sender, request, user, **kwargs):
    """Let the weekly email find this account's marks from now on.

    A reader who signed up anonymously and later made an account would
    otherwise keep getting an email built from a cookie whose marks this very
    sign-in has just moved onto the account.
    """
    from digest.services import link_user

    link_user(user)
