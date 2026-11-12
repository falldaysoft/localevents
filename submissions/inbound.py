"""Event submissions by email: the mailbox, the message, and what becomes of it.

The path is: `poll_inbox` reads unseen mail over IMAP and stores each message
as an `InboundEmail`, then the worker triages it (`enrichment.triage`) and
anything that is an event goes through the API's own validation into
`save_event_from_draft` — so an emailed listing meets every rule a typed one
does, the all-past-dates guard included.

Like the API, nobody confirms an emailed listing on the site before a
moderator sees it. What stands in for that: the queue says "by email" and
shows the message itself, triage's verdict is reviewable under the inbox,
and a sender gets one account however many times they write, so their mail
lands in "My events" and the decision mail reaches them.

Two loop guards matter more than they look. Mail that a machine sent is
triaged but never answered, and mail from our own addresses is dropped — two
auto-responders replying to each other will otherwise do so until a quota
somewhere runs out.
"""

import email
import hashlib
import imaplib
import logging
import re
from datetime import timedelta
from email import policy
from email.utils import getaddresses, make_msgid, parseaddr

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import EmailMessage
from django.db import IntegrityError, transaction
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from .models import InboundEmail, Submission, SubmissionQuota

logger = logging.getLogger("submissions.inbound")

MAX_BODY_CHARS = 20_000
# Per poll. A backlog drains over a few runs rather than one long one.
MAX_MESSAGES_PER_POLL = 50
# Triage costs money and a model's attention per message, so one address
# flooding the inbox is stopped before triage, not after.
MAX_EMAILS_PER_SENDER_PER_DAY = 20
# Answers per address per day. Enough for "got it" and "we need a date";
# few enough that a spoofed From cannot turn us into a mail cannon.
MAX_REPLIES_PER_SENDER_PER_DAY = 3


def mailbox():
    """Where submissions arrive: the admin's Mail configuration, else .env."""
    from core.models import MailConfig

    return MailConfig.load().inbound()


def is_configured():
    return mailbox().configured


# ---------------------------------------------------------------------------
# Reading a message
# ---------------------------------------------------------------------------


def _own_addresses():
    from core.models import MailConfig

    box = mailbox()
    addresses = {
        parseaddr(settings.DEFAULT_FROM_EMAIL)[1],
        parseaddr(settings.SERVER_EMAIL)[1],
        parseaddr(MailConfig.load().resolved_from_email())[1],
        box.address,
        box.user,
    }
    return {a.lower() for a in addresses if a and "@" in a}


def sender_check(message, raw=None, dnsfunc=None):
    """Did the sender's domain vouch for the From address?

    The receiving provider's verdict wins when it gave one. Only the topmost
    Authentication-Results header counts: it is the one our own provider
    added on receipt, and anything below it arrived with the message, where
    a spoofer can write whatever they like.

    Not every provider records one — DreamHost does not — so without it we
    check DKIM ourselves: a valid signature from the From address's own
    domain is the same proof DMARC's DKIM half asks for. A missing or broken
    signature is "not verified", never "fail": forwarding and mailing lists
    break signatures on perfectly honest mail, and only a DMARC policy can
    say a failure means forgery.
    """
    header = message.get("Authentication-Results")
    if header:
        found = re.search(r"\bdmarc=(\w+)", str(header), re.IGNORECASE)
        if found and found.group(1).lower() == "pass":
            return InboundEmail.SenderCheck.PASS
        if found and found.group(1).lower() == "fail":
            return InboundEmail.SenderCheck.FAIL
    if raw is not None and _dkim_aligned(message, raw, dnsfunc):
        return InboundEmail.SenderCheck.PASS
    return InboundEmail.SenderCheck.NONE


def _dkim_aligned(message, raw, dnsfunc=None):
    """Is there a valid DKIM signature whose d= is the From domain?

    Relaxed alignment, approximately: the signing domain may be the From
    domain or a parent of it (mail.club.example signed by club.example).
    """
    import dkim

    from_domain = parseaddr(str(message.get("From", "")))[1].rpartition("@")[2].lower()
    signatures = message.get_all("DKIM-Signature") or []
    if not from_domain or not signatures:
        return False
    checker = dkim.DKIM(raw)
    for index, signature in enumerate(signatures[:5]):
        found = re.search(r"(?:^|;)\s*d\s*=\s*([^;\s]+)", str(signature))
        signer = found.group(1).lower().rstrip(".") if found else ""
        if not signer or not (
            from_domain == signer or from_domain.endswith("." + signer)
        ):
            continue
        try:
            kwargs = {"dnsfunc": dnsfunc} if dnsfunc else {}
            if checker.verify(idx=index, **kwargs):
                return True
        except Exception as exc:
            # A DNS timeout or a malformed signature is "not verified",
            # never a reason to lose the message.
            logger.info("DKIM check failed for %s: %s", from_domain, exc)
    return False


def _is_bounce(message, address):
    local = address.split("@")[0].lower()
    if local in {"mailer-daemon", "postmaster"}:
        return True
    return message.get_content_type() == "multipart/report"


def _is_automated(message):
    auto = str(message.get("Auto-Submitted", "no")).strip().lower()
    if auto and auto != "no":
        return True
    if str(message.get("Precedence", "")).strip().lower() in {"bulk", "list", "junk"}:
        return True
    return bool(message.get("List-Id") or message.get("List-Unsubscribe"))


def _body_text(message):
    from enrichment.pipeline import page_text

    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, UnicodeError):
        payload = part.get_payload(decode=True) or b""
        content = payload.decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        content = page_text(content)
    return content.strip()[:MAX_BODY_CHARS]


def _attachment_names(message):
    names = []
    for part in message.iter_attachments():
        name = part.get_filename()
        if name:
            names.append(name[:200])
    return names[:20]


def store(raw):
    """Keep one raw message as an InboundEmail. Returns it, or None if seen.

    Idempotent on Message-ID, which is what makes the mailbox poll safe to
    die halfway through: a message stored but not yet flagged read comes back
    on the next poll and is recognised.
    """
    message = email.message_from_bytes(raw, policy=policy.default)

    message_id = str(message.get("Message-ID", "")).strip()
    key = message_id[:255] or "sha256:" + hashlib.sha256(raw).hexdigest()
    if InboundEmail.objects.filter(message_key=key).exists():
        return None

    name, address = parseaddr(str(message.get("From", "")))
    # Reply-To is where a person wants answers, and From is often a list or
    # a "via" relay for exactly the senders most likely to write in.
    reply_to = getaddresses([str(message.get("Reply-To", ""))])
    address = address.strip().lower()

    inbound = InboundEmail(
        message_key=key,
        from_address=address[:254],
        from_name=name[:200],
        subject=str(message.get("Subject", ""))[:300],
        body=_body_text(message),
        attachment_names=_attachment_names(message),
        sender_check=sender_check(message, raw),
        is_automated=_is_automated(message),
    )

    if not address or "@" not in address:
        inbound.status = InboundEmail.Status.IGNORED
        inbound.triage_reason = "No usable From address."
    elif address in _own_addresses() or (
        reply_to and reply_to[0][1].lower() in _own_addresses()
    ):
        inbound.status = InboundEmail.Status.IGNORED
        inbound.triage_reason = "Sent from this site's own address."
    elif _is_bounce(message, address):
        inbound.status = InboundEmail.Status.IGNORED
        inbound.triage_reason = "A bounce or delivery report."
    elif inbound.sender_check == InboundEmail.SenderCheck.FAIL:
        # Free to decide, and the commonest spam of all: someone else's
        # domain, which that domain says it did not send.
        inbound.status = InboundEmail.Status.SPAM
        inbound.triage_reason = (
            "The sender's domain says it did not send this (DMARC failed)."
        )
    elif _over_sender_limit(address):
        inbound.status = InboundEmail.Status.LIMITED
        inbound.triage_reason = (
            f"More than {MAX_EMAILS_PER_SENDER_PER_DAY} emails from this "
            "address today; not read."
        )

    try:
        with transaction.atomic():
            inbound.save()
    except IntegrityError:
        return None
    return inbound


def _over_sender_limit(address):
    since = timezone.now() - timedelta(days=1)
    return (
        InboundEmail.objects.filter(from_address=address, received_at__gte=since)
        .exclude(status=InboundEmail.Status.IGNORED)
        .count()
        >= MAX_EMAILS_PER_SENDER_PER_DAY
    )


def receive(raw):
    """Store a message and, if it needs reading, queue its triage."""
    from .tasks import triage_inbound_email

    inbound = store(raw)
    if inbound is not None and inbound.status == InboundEmail.Status.RECEIVED:
        triage_inbound_email.enqueue(inbound.pk)
    return inbound


# ---------------------------------------------------------------------------
# The mailbox
# ---------------------------------------------------------------------------


def poll(connect=None):
    """Read unseen mail from the IMAP folder. Returns how many were stored.

    `BODY.PEEK[]` fetches without marking read; a message is flagged \\Seen
    only once it is safely stored, so a crash between the two re-reads it
    next time and `store` recognises it.
    """
    box = mailbox()
    if not box.configured:
        return 0

    connect = connect or _connect
    imap = connect(box)
    stored = 0
    try:
        status, _ = imap.select(box.folder)
        if status != "OK":
            raise RuntimeError(f"Could not open {box.folder}")
        status, data = imap.uid("SEARCH", None, "UNSEEN")
        if status != "OK":
            raise RuntimeError("IMAP search failed")
        uids = (data[0] or b"").split()[:MAX_MESSAGES_PER_POLL]
        for uid in uids:
            status, parts = imap.uid("FETCH", uid, "(BODY.PEEK[])")
            raw = next(
                (p[1] for p in parts or [] if isinstance(p, tuple) and len(p) > 1),
                None,
            )
            if status != "OK" or raw is None:
                logger.warning("could not fetch message uid %s", uid)
                continue
            try:
                if receive(raw) is not None:
                    stored += 1
            except Exception:
                # One malformed message must not stall the whole mailbox. It
                # stays unread, so it is retried — and logged each time.
                logger.exception("could not store message uid %s", uid)
                continue
            imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return stored


def _connect(box):
    imap = imaplib.IMAP4_SSL(box.host, box.port, timeout=60)
    imap.login(box.user, box.password)
    return imap


def test_mailbox(box=None, connect=None):
    """Sign in and count unread mail, without reading or flagging any.

    For the admin's Test button. Returns a sentence either way; never raises,
    because the error *is* the answer the person pressing it needs.
    """
    box = box or mailbox()
    if not box.configured:
        return False, "No IMAP host and user are set, here or in the environment."
    try:
        imap = (connect or _connect)(box)
    except Exception as exc:
        return False, f"Could not sign in to {box.host}:{box.port} as {box.user}: {_imap_error(exc)}"
    try:
        status, _ = imap.select(box.folder, readonly=True)
        if status != "OK":
            return False, f"Signed in, but there is no folder called “{box.folder}”."
        status, data = imap.uid("SEARCH", None, "UNSEEN")
        unseen = len((data[0] or b"").split()) if status == "OK" else 0
    except Exception as exc:
        return False, f"Signed in, but reading “{box.folder}” failed: {_imap_error(exc)}"
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return True, (
        f"Signed in to {box.host} as {box.user}. “{box.folder}” has "
        f"{unseen} unread message{'s' if unseen != 1 else ''}, which the next "
        "poll will read."
    )


def _imap_error(exc):
    detail = exc.args[0] if exc.args else exc
    if isinstance(detail, bytes):
        detail = detail.decode("utf-8", errors="replace")
    return str(detail) or type(exc).__name__


# ---------------------------------------------------------------------------
# From a verdict to the queue
# ---------------------------------------------------------------------------


def sender_account(address, name=""):
    """The account an emailed submission belongs to, made if need be.

    Matching on the address means someone who already has an account sees
    the listing in "My events" and gets the decision mail as usual. A new
    sender gets an account with no password and an unverified address: it
    exists so the decision mail has somewhere to go and so their later
    emails gather in one place. They can take it over with "forgot
    password", which proves they hold the address.
    """
    from allauth.account.models import EmailAddress

    User = get_user_model()
    user = User.objects.filter(email__iexact=address).first()
    if user is None:
        known = EmailAddress.objects.filter(email__iexact=address).first()
        user = known.user if known else None
    if user is not None:
        return user

    user = User(username=_free_username(name, address), email=address)
    user.set_unusable_password()
    user.save()
    EmailAddress.objects.create(
        user=user, email=address, primary=True, verified=False
    )
    return user


def _free_username(name, address):
    """A display name from the sender's name, never from their address.

    The username is printed against a listing, and the local part of an
    email address is half of the address.
    """
    User = get_user_model()
    base = re.sub(r"[^\w.+-]+", "_", name.strip()).strip("_.")[:30]
    if not base or "@" in name:
        base = "email_submitter"
    candidate = base
    suffix = 1
    while User.objects.filter(username__iexact=candidate).exists():
        suffix += 1
        candidate = f"{base}{suffix}"
    return candidate


def process(inbound, *, force=False):
    """Triage one stored email and act on the verdict.

    `force` is a moderator saying "this is an event" about something triage
    turned away; it re-reads the email with that settled.
    """
    from enrichment.pipeline import enrich_url
    from enrichment.triage import triage_email

    result = triage_email(inbound, force=force)
    if result.failed:
        inbound.status = InboundEmail.Status.FAILED
        inbound.triage_reason = result.error[:500]
        inbound.save(update_fields=["status", "triage_reason", "updated_at"])
        return inbound

    triage = result.triage
    inbound.triage_reason = triage.reason[:500]
    if triage.verdict == "spam":
        inbound.status = InboundEmail.Status.SPAM
    elif triage.verdict == "not_event":
        inbound.status = InboundEmail.Status.NOT_EVENT
    if triage.verdict != "event":
        inbound.save(update_fields=["status", "triage_reason", "updated_at"])
        return inbound

    drafts = list(triage.events)
    source_url = _valid_url(triage.event_page_url)
    if source_url and not any(d.occurrences for d in drafts):
        # "Please list our event: <link>" — the details are on the page, so
        # read it the way the web form would, through the SSRF-safe fetcher.
        page = enrich_url(source_url)
        if page.draft is not None:
            drafts = [page.draft]

    user = sender_account(inbound.from_address, inbound.from_name)
    quota = SubmissionQuota.for_user(user)
    queued, problems = [], []
    for draft in drafts:
        if not quota.may_submit():
            problems.append(f"“{draft.title}”: the sender's daily limit is reached.")
            break
        submission, errors = _queue(inbound, user, draft, source_url)
        if submission is not None:
            queued.append(submission)
        else:
            problems.append(f"“{draft.title}”: {errors}")

    if queued:
        inbound.status = InboundEmail.Status.QUEUED
    else:
        inbound.status = InboundEmail.Status.INCOMPLETE
        if not drafts:
            problems.append("No event details could be read from it.")
    if problems:
        inbound.triage_reason = (
            inbound.triage_reason + " Not queued: " + " ".join(problems)
        ).strip()[:500]
    inbound.save(update_fields=["status", "triage_reason", "updated_at"])

    _reply(inbound, queued, problems)
    return inbound


def _queue(inbound, user, draft, source_url):
    """Validate one draft as the API would, and queue it. (submission, errors)."""
    from .api import _listing_from_draft, _validate
    from .services import save_event_from_draft

    submission = Submission(
        submitted_by=user,
        inbound_email=inbound,
        source_url=_valid_url(source_url),
        pasted_text=_as_source_text(inbound),
        draft=draft.model_dump(mode="json"),
        enrichment_message="Read from an email by triage.",
    )
    listing = _listing_from_draft(submission)
    cleaned, errors = _validate(listing)
    if errors and set(errors) <= DISPENSABLE_FIELDS:
        # A garbled ticket link is no reason to turn away the event it was
        # attached to. The moderator sees the email and can add it back.
        for name in errors:
            listing.pop(name, None)
        cleaned, errors = _validate(listing)
    if errors:
        return None, _summarise(errors)

    with transaction.atomic():
        submission.status = Submission.Status.AWAITING_SUBMITTER
        submission.save()
        save_event_from_draft(submission, cleaned)
    return submission, ""


# Fields a listing can lose without becoming a different listing.
DISPENSABLE_FIELDS = {
    "summary", "description", "venue_address", "organizer_name",
    "price_note", "accessibility_notes", "source_url", "ticket_url",
    "categories",
}


def _valid_url(value):
    from django.core.exceptions import ValidationError
    from django.core.validators import URLValidator

    value = (value or "").strip()
    if not value or len(value) > 200:
        return ""
    try:
        URLValidator(schemes=["http", "https"])(value)
    except ValidationError:
        return ""
    return value


def _as_source_text(inbound):
    header = f"From: {inbound.from_name} <{inbound.from_address}>\nSubject: {inbound.subject}\n"
    if inbound.attachment_names:
        header += f"Attachments: {', '.join(inbound.attachment_names)}\n"
    return (header + "\n" + inbound.body)[:MAX_BODY_CHARS]


def _summarise(errors):
    if "occurrences" in errors or "occurrences_rows" in errors:
        messages = errors.get("occurrences") or ["a date could not be read"]
        return " ".join(str(m) for m in messages)
    first = next(iter(errors.items()))
    return f"{first[0]}: {' '.join(str(m) for m in first[1])}"


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------


def _may_reply(inbound):
    if inbound.is_automated or inbound.sender_check == InboundEmail.SenderCheck.FAIL:
        return False
    since = timezone.now() - timedelta(days=1)
    sent = InboundEmail.objects.filter(
        from_address=inbound.from_address, replied_at__gte=since
    ).count()
    return sent < MAX_REPLIES_PER_SENDER_PER_DAY


def _reply(inbound, queued, problems):
    if not _may_reply(inbound):
        return
    from moderation.tasks import absolute_url

    kind = "received" if queued else "incomplete"
    context = {
        "inbound": inbound,
        "queued": queued,
        "problems": problems,
        "site_name": settings.SITE_NAME,
        "submit_url": absolute_url(reverse("submit")),
        "mine_url": absolute_url(reverse("my_events")),
        "reset_url": absolute_url(reverse("account_reset_password")),
        "has_password": queued and queued[0].submitted_by.has_usable_password(),
    }
    subject = inbound.subject or "your email"
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    headers = {
        # RFC 3834: tells the sender's own auto-responder not to answer us.
        "Auto-Submitted": "auto-replied",
        "Message-ID": make_msgid(domain=settings.ALLOWED_HOSTS[0]),
    }
    if not inbound.message_key.startswith("sha256:"):
        headers["In-Reply-To"] = inbound.message_key
        headers["References"] = inbound.message_key
    address = mailbox().address
    message = EmailMessage(
        subject=subject[:200],
        body=render_to_string(f"submissions/email/{kind}.txt", context),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[inbound.from_address],
        reply_to=[address] if address else None,
        headers=headers,
    )
    try:
        message.send()
    except Exception:
        # The listing is already queued; a relay being down must not undo it.
        logger.exception("could not answer inbound email %s", inbound.pk)
        return
    inbound.replied_at = timezone.now()
    inbound.save(update_fields=["replied_at", "updated_at"])
