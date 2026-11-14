"""Every way an address joins or leaves the weekly email.

Views call these and nothing else writes `Subscriber`, so the consent record
is kept the same way whichever door someone came in by.
"""

from datetime import timedelta

from django.core import signing
from django.db import transaction
from django.utils import timezone

from .models import Subscriber, normalize_email

CONFIRM_SALT = "digest.confirm"
CONFIRM_MAX_AGE = timedelta(days=7)
# A form someone can submit with any address must not become a way to fill a
# stranger's inbox: at most one confirmation per address in this window,
# however many times the form is posted.
RESEND_AFTER = timedelta(minutes=15)


def confirmation_key(subscriber):
    """The signed value in a confirmation link.

    It carries the address as well as the id, so a link sent to one address
    cannot confirm a row that has since been changed to another.
    """
    return signing.dumps(
        {"id": subscriber.pk, "email": subscriber.email}, salt=CONFIRM_SALT
    )


def subscriber_for_key(key):
    """The row a confirmation link names, or None if it is bad or expired."""
    try:
        data = signing.loads(
            key, salt=CONFIRM_SALT, max_age=CONFIRM_MAX_AGE.total_seconds()
        )
    except signing.BadSignature:
        return None
    return Subscriber.objects.filter(pk=data.get("id"), email=data.get("email")).first()


def verified_email(user):
    """The user's address if allauth has verified it, else None."""
    from allauth.account.models import EmailAddress

    if not user or not user.is_authenticated or not user.email:
        return None
    verified = EmailAddress.objects.filter(
        user=user, email__iexact=user.email, verified=True
    ).exists()
    return normalize_email(user.email) if verified else None


def request_subscription(email, fingerprint="", user=None):
    """Someone asked, through the form, for this address to get the email.

    Returns nothing the caller should show: the page says "check your inbox"
    whatever happened, so the form cannot be used to learn who subscribes.

    An address that is already receiving is left exactly as it is — in
    particular its fingerprint is not replaced, or anyone could attach their
    own marks to a stranger's email. A signed-in reader typing their own
    verified address is subscribed at once, since the confirmation would only
    prove what allauth already has.
    """
    email = normalize_email(email)
    if user is not None and verified_email(user) == email:
        subscribe_user(user, fingerprint=fingerprint)
        return

    with transaction.atomic():
        subscriber, created = Subscriber.objects.select_for_update().get_or_create(
            email=email,
            defaults={"source": Subscriber.Source.FORM, "fingerprint": fingerprint},
        )
        if subscriber.is_active:
            return
        now = timezone.now()
        if (
            not created
            and subscriber.confirmation_sent_at
            and now - subscriber.confirmation_sent_at < RESEND_AFTER
        ):
            return
        if fingerprint:
            subscriber.fingerprint = fingerprint
        subscriber.source = Subscriber.Source.FORM
        subscriber.confirmation_sent_at = now
        subscriber.save(
            update_fields=["fingerprint", "source", "confirmation_sent_at"]
        )

    from .tasks import send_confirmation

    pk = subscriber.pk
    transaction.on_commit(lambda: send_confirmation.enqueue(pk))


def confirm(subscriber):
    subscriber.activate("Confirmed by the link in the confirmation email.")


def subscribe_user(user, fingerprint=""):
    """A signed-in reader with a verified address turned the email on.

    Returns the row, or None when the address is not verified — the profile
    hides the switch in that case, so None only reaches a hand-made POST.
    """
    email = verified_email(user)
    if email is None:
        return None
    with transaction.atomic():
        subscriber, _ = Subscriber.objects.select_for_update().get_or_create(
            email=email, defaults={"source": Subscriber.Source.PROFILE}
        )
        # The account's marks are what personalise the email from here on;
        # an address that belonged to nobody before belongs to this account.
        changed = []
        if subscriber.user_id != user.pk:
            Subscriber.objects.filter(user=user).exclude(pk=subscriber.pk).update(
                user=None
            )
            subscriber.user = user
            changed.append("user")
        if fingerprint and not subscriber.fingerprint:
            subscriber.fingerprint = fingerprint
            changed.append("fingerprint")
        if not subscriber.is_active:
            subscriber.source = Subscriber.Source.PROFILE
            changed.append("source")
        if changed:
            subscriber.save(update_fields=changed)
        subscriber.activate("Turned on while signed in with a verified address.")
    return subscriber


def unsubscribe(subscriber, delivery_id=None):
    """Stop the email, noting which week's email the link came from.

    The delivery id rides in the link's query string, so it is only believed
    when it names a delivery that was actually sent to this subscriber.
    """
    via = None
    if delivery_id and str(delivery_id).isdigit():
        via = subscriber.deliveries.filter(pk=int(delivery_id)).first()
    subscriber.deactivate(via=via)


def link_user(user):
    """Attach a subscription to the account that owns its address.

    Called on every sign-in. Only an unclaimed row is taken, and only by the
    account whose address it is: signing in requires a verified address, so
    the match is proof enough — checked anyway, since an account made from an
    inbound email exists before its address is.
    """
    email = verified_email(user)
    if not email or Subscriber.objects.filter(user=user).exists():
        return
    Subscriber.objects.filter(email=email, user__isnull=True).update(user=user)


# --- moderators ------------------------------------------------------------

# A pending signup nobody confirmed is an address someone typed, perhaps not
# their own; past this it is forgotten rather than kept indefinitely.
FORGET_UNCONFIRMED_AFTER = timedelta(days=30)


def _account_for(email):
    from allauth.account.models import EmailAddress

    row = (
        EmailAddress.objects.filter(email__iexact=email, verified=True)
        .select_related("user")
        .first()
    )
    return row.user if row else None


def moderator_subscribe(actor, email, note):
    """Subscribe an address on someone's behalf, at once, without a link.

    For a person who asked by phone, at a stall, by replying to an email.
    The moderator is vouching for that request, so there is no confirmation
    step — and the note saying how they asked is required, because it is
    the consent record. Returns `(subscriber, changed)`; an address already
    receiving is left alone.
    """
    from submissions.models import ModerationAction

    email = normalize_email(email)
    note = note.strip()
    with transaction.atomic():
        subscriber, _ = Subscriber.objects.select_for_update().get_or_create(
            email=email, defaults={"source": Subscriber.Source.MODERATOR}
        )
        if subscriber.is_active:
            return subscriber, False
        subscriber.source = Subscriber.Source.MODERATOR
        changed = ["source"]
        if subscriber.user_id is None:
            user = _account_for(email)
            if user is not None and not Subscriber.objects.filter(user=user).exists():
                subscriber.user = user
                changed.append("user")
        subscriber.save(update_fields=changed)
        subscriber.activate(f"Added by {actor.display_name}: {note}")
        ModerationAction.record(
            actor, "weekly email: subscribed", detail=f"{email} — {note}"
        )
    return subscriber, True


def moderator_unsubscribe(actor, subscriber, note):
    """Stop the email for someone who asked outside the email's own link."""
    from submissions.models import ModerationAction

    if subscriber.unsubscribed_at:
        return False
    note = note.strip()
    with transaction.atomic():
        subscriber.deactivate(note=f"Removed by {actor.display_name}: {note}")
        ModerationAction.record(
            actor, "weekly email: unsubscribed", detail=f"{subscriber.email} — {note}"
        )
    return True


def set_enabled(actor, on):
    """The master switch, from the moderator page. Recorded, since it mails everyone."""
    from core.models import SiteConfig
    from submissions.models import ModerationAction

    config = SiteConfig.load()
    if config.digest_enabled == on:
        return False
    config.digest_enabled = on
    config.save(update_fields=["digest_enabled"])
    ModerationAction.record(
        actor, "weekly email: switched on" if on else "weekly email: switched off"
    )
    return True


def forget_unconfirmed(now=None):
    """Delete signups never confirmed within `FORGET_UNCONFIRMED_AFTER`.

    Only rows that never went anywhere: an address that once received the
    email and left keeps its row, which is the record that it said stop.
    """
    cutoff = (now or timezone.now()) - FORGET_UNCONFIRMED_AFTER
    stale = (
        Subscriber.objects.pending()
        .filter(created_at__lt=cutoff)
        .exclude(confirmation_sent_at__gte=cutoff)
    )
    count, _ = stale.delete()
    return count
