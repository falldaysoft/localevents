"""Sending the weekly email: when it is due, who it goes to, and the sending.

Nobody presses a button. Housekeeping runs hourly and calls `start_due_run`;
on the configured local weekday, from the configured hour on, the first call
creates that day's `DigestRun` and a `DigestDelivery` per active subscriber,
then queues the deliveries in batches. Every later call that day finds the
run and does nothing but re-queue deliveries stranded by a worker restart.

A delivery is rendered and sent under a lock on its own row and only while
it is still PENDING, so the same batch run twice sends nothing twice. One
reader's failure is recorded on their row and the batch carries on.
"""

import email.policy
import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMultiAlternatives, get_connection
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from core.models import SiteConfig

from . import content
from .models import DigestDelivery, DigestRun, Subscriber

logger = logging.getLogger("digest.sending")

BATCH_SIZE = 50
# A batch queued this long ago whose deliveries are still waiting was lost
# with its worker; housekeeping queues it again.
STRANDED_AFTER = timedelta(minutes=30)


def is_due(now, config):
    local = timezone.localtime(now)
    return (
        config.digest_enabled
        and local.weekday() == config.digest_weekday
        and local.hour >= config.digest_hour
    )


def start_due_run(now=None):
    """Start this week's run if it is due and not started. Returns the run."""
    now = now or timezone.now()
    # Before the due check, so a run whose worker died late on sending day is
    # still finished the morning after. Switched off, the re-queued batch
    # skips what is left rather than sending it (see `send_batch`).
    for unfinished in DigestRun.objects.filter(finished_at__isnull=True):
        requeue_stranded(unfinished, now)

    config = SiteConfig.load()
    if not is_due(now, config):
        return None

    send_date = timezone.localdate(now)
    existing = DigestRun.objects.filter(send_date=send_date).first()
    if existing is not None:
        return existing

    record = _default_copy(now)
    try:
        with transaction.atomic():
            run = DigestRun.objects.create(
                send_date=send_date,
                active_subscribers=Subscriber.objects.active().count(),
                **record,
            )
            DigestDelivery.objects.bulk_create(
                DigestDelivery(run=run, subscriber=s, email=s.email)
                for s in Subscriber.objects.active().only("pk", "email")
            )
    except IntegrityError:
        # Another process created it between the check and the insert.
        return DigestRun.objects.get(send_date=send_date)

    ids = list(run.deliveries.values_list("pk", flat=True))
    logger.info("weekly email %s: %s deliveries", send_date, len(ids))
    if not ids:
        _finish_if_done(run)
    transaction.on_commit(lambda: _enqueue(run.pk, ids))
    return run


def _default_copy(now):
    """The run's stored copy of the plain email, or nothing if it won't build.

    A record of the week is worth having and not worth missing the week for:
    anything that breaks the plain render would break every reader's too, and
    that is reported per delivery, where someone will see it.
    """
    try:
        digest = content.render_default(now)
    except Exception:
        logger.exception("weekly email %s: could not keep a copy", now.date())
        return {}
    return {
        "subject": digest.subject[:300],
        "text": digest.text,
        "html": digest.html,
        "event_count": digest.event_count,
    }


def _enqueue(run_id, ids):
    from .tasks import send_digest_batch

    for start in range(0, len(ids), BATCH_SIZE):
        send_digest_batch.enqueue(run_id, ids[start : start + BATCH_SIZE])


def requeue_stranded(run, now):
    if run.finished_at or now - run.started_at < STRANDED_AFTER:
        return
    ids = list(
        run.deliveries.filter(status=DigestDelivery.Status.PENDING).values_list(
            "pk", flat=True
        )
    )
    if ids:
        logger.warning("weekly email %s: re-queueing %s", run.send_date, len(ids))
        _enqueue(run.pk, ids)
    else:
        _finish_if_done(run)


def unsubscribe_url(subscriber, delivery=None):
    path = reverse("digest_unsubscribe", args=[subscriber.token])
    if delivery is not None:
        path += f"?d={delivery.pk}"
    return content.absolute_url(path)


class DigestMessage(EmailMultiAlternatives):
    """Keeps `List-Unsubscribe` readable by the mail providers that act on it.

    Python's email policy wraps headers at 78 characters, and an unsubscribe
    URL is one long unbreakable word — so it falls back to RFC 2047 encoded
    words, `=?utf-8?q?=3Chttps=3A//...`, which RFC 2369 does not allow inside
    the angle brackets and which a provider may simply fail to parse. Seen in
    the first real run. 998 is the hard line limit in RFC 5322; whatever
    policy the backend asks for (SMTP's CRLF included) is kept otherwise.
    """

    def message(self, *, policy=email.policy.default):
        return super().message(policy=policy.clone(max_line_length=998))


def build_message(subscriber, digest, list_unsubscribe, subject_prefix=""):
    """The email as it leaves: both parts, and the headers bulk mail owes.

    `List-Unsubscribe` with `List-Unsubscribe-Post` is RFC 8058's one-click
    unsubscribe — Gmail and Yahoo require it of bulk senders and show their
    own button for it. Replies go to a person, never to the submissions
    inbox, where triage would read them as malformed events.
    """
    message = DigestMessage(
        subject=subject_prefix + digest.subject,
        body=digest.text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[subscriber.email],
        reply_to=[settings.CONTACT_EMAIL],
        headers={
            "List-Unsubscribe": f"<{list_unsubscribe}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            "Precedence": "bulk",
            "Auto-Submitted": "auto-generated",
        },
    )
    message.attach_alternative(digest.html, "text/html")
    return message


def send_batch(run_id, delivery_ids):
    run = DigestRun.objects.filter(pk=run_id).first()
    if run is None:
        return
    config = SiteConfig.load()
    if not config.digest_enabled:
        # The switch is how a moderator stops a run that has gone wrong —
        # a broken template, the wrong week — so it holds mid-run. Skipped,
        # not left waiting: a run resumed days later would send stale news.
        run.deliveries.filter(
            pk__in=delivery_ids, status=DigestDelivery.Status.PENDING
        ).update(
            status=DigestDelivery.Status.SKIPPED,
            detail="The weekly email was switched off before this was sent.",
        )
        _finish_if_done(run)
        return
    min_events = config.digest_min_events
    now = timezone.now()
    bases = {}
    connection = get_connection()
    # Opened once for the batch. If the relay is down this raises, the
    # deliveries stay PENDING, and housekeeping re-queues them later.
    connection.open()
    try:
        for pk in delivery_ids:
            _send_one(pk, now, min_events, bases, connection)
    finally:
        connection.close()
    _finish_if_done(run)


def _send_one(pk, now, min_events, bases, connection):
    Status = DigestDelivery.Status
    with transaction.atomic():
        # `of=("self",)`: `subscriber` is nullable, so the join is an outer
        # one, and Postgres refuses FOR UPDATE on its nullable side. SQLite
        # ignores FOR UPDATE, so only a Postgres run catches this.
        delivery = (
            DigestDelivery.objects.select_for_update(of=("self",))
            .select_related("subscriber")
            .filter(pk=pk, status=Status.PENDING)
            .first()
        )
        if delivery is None:
            return
        subscriber = delivery.subscriber
        if subscriber is None or not subscriber.is_active:
            delivery.status = Status.SKIPPED
            delivery.detail = "Unsubscribed before it was sent."
            delivery.save(update_fields=["status", "detail"])
            return

        try:
            filters = content.filter_for(subscriber)
            key = repr(filters)
            if key not in bases:
                bases[key] = content.base_listing(filters, now)
            link = unsubscribe_url(subscriber, delivery)
            digest = content.render_for(
                subscriber, now=now, base=bases[key], unsubscribe_url=link
            )
            delivery.subject = digest.subject[:300]
            delivery.event_count = digest.event_count
            delivery.favorites_count = digest.favorites_count
            if digest.event_count < min_events:
                delivery.status = Status.SKIPPED
                delivery.detail = (
                    f"Only {digest.event_count} event(s); the minimum is {min_events}."
                )
            else:
                connection.send_messages([build_message(subscriber, digest, link)])
                delivery.status = Status.SENT
                delivery.sent_at = timezone.now()
                Subscriber.objects.filter(pk=subscriber.pk).update(
                    last_sent_at=delivery.sent_at
                )
        except Exception as exc:
            logger.exception("weekly email to %s failed", delivery.email)
            delivery.status = Status.FAILED
            delivery.detail = f"{type(exc).__name__}: {exc}"[:300]
        delivery.save()


def _finish_if_done(run):
    if run.finished_at:
        return
    if not run.deliveries.filter(status=DigestDelivery.Status.PENDING).exists():
        DigestRun.objects.filter(pk=run.pk, finished_at__isnull=True).update(
            finished_at=timezone.now()
        )


def send_test(user, fingerprint=""):
    """Send `user` the email they would get now, marked as a test.

    For a moderator checking what readers will see. Built for the user's own
    subscription if they have one, otherwise as a fresh reader with their
    account's stars. Not a delivery: it is in no run and no count, and the
    minimum-events rule does not apply, since an empty week is exactly the
    thing worth previewing.
    """
    from .models import normalize_email

    email = normalize_email(user.email)
    subscriber = Subscriber.objects.filter(email=email).first()
    if subscriber is not None:
        link = unsubscribe_url(subscriber)
    else:
        subscriber = Subscriber(email=email, user=user, fingerprint=fingerprint)
        link = content.absolute_url(reverse("digest_signup"))
    digest = content.render_for(subscriber, unsubscribe_url=link)
    build_message(subscriber, digest, link, subject_prefix="[Test] ").send()
    return digest
