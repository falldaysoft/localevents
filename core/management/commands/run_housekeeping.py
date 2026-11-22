"""Periodic maintenance, run from the Helm CronJob.

One command rather than several so the schedule has a single entry point. Each
step is independent and failure-isolated: a broken import source must not stop
series expiry from running.
"""

import logging
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger("core.housekeeping")


class Command(BaseCommand):
    help = "Run periodic maintenance: poll import sources, expire stale series."

    def handle(self, *args, **options):
        steps = [
            ("recover stranded submissions", self.recover_stranded_submissions),
            ("geocode pending venues", self.geocode_pending_venues),
            ("poll import sources", self.poll_import_sources),
            ("read the submissions mailbox", self.poll_inbox),
            ("expire stale series", self.expire_series),
            ("send renewal reminders", self.send_renewal_reminders),
            ("send the weekly email", self.send_weekly_email),
            ("forget unconfirmed weekly-email signups", self.forget_unconfirmed_signups),
            ("tidy sponsorship orders", self.tidy_sponsorships),
        ]

        failures = 0
        for label, step in steps:
            try:
                step()
            except Exception:
                failures += 1
                logger.exception("housekeeping step failed: %s", label)
                self.stderr.write(self.style.ERROR(f"{label}: failed"))
            else:
                self.stdout.write(f"{label}: ok")

        if failures:
            raise SystemExit(1)

    # Each of these is filled in by a later phase. They are no-ops rather than
    # NotImplementedError so the schedule can be wired up and verified early.

    def recover_stranded_submissions(self):
        """Release submissions whose enrichment worker died.

        The view recovers one when its owner next looks at it, but that only
        helps people who come back. This catches the rest — and matters most
        right after a deploy, which is exactly when workers get restarted
        mid-task.
        """
        from submissions.models import Submission

        stranded = list(Submission.objects.stranded())
        for submission in stranded:
            submission.recover_from_stranding()
        if stranded:
            logger.info("recovered %s stranded submission(s)", len(stranded))
            self.stdout.write(f"  recovered {len(stranded)} stranded submission(s)")

    def geocode_pending_venues(self):
        """Pick up venues whose geocode never ran or was asked for again.

        A venue is enqueued when it is created, but a task lost to a worker
        restart, or a venue marked pending in the admin, otherwise sits with no
        marker forever. The sweep is bounded inside the task.
        """
        from events.geocoding import geocode_pending_venues

        geocode_pending_venues.enqueue()

    def poll_import_sources(self):
        return

    def poll_inbox(self):
        """A backstop for the five-minute crontab line, not a replacement.

        Also re-queues triage for anything stored but never read — a worker
        restart between the two is the same stranding as enrichment's.
        """
        from submissions import inbound
        from submissions.models import InboundEmail
        from submissions.tasks import triage_inbound_email

        if inbound.is_configured():
            stored = inbound.poll()
            self.stdout.write(f"  stored {stored} new message(s)")
        cutoff = timezone.now() - timedelta(minutes=30)
        for pk in InboundEmail.objects.filter(
            status=InboundEmail.Status.RECEIVED, updated_at__lt=cutoff
        ).values_list("pk", flat=True):
            triage_inbound_email.enqueue(pk)

    def expire_series(self):
        return

    def send_renewal_reminders(self):
        return

    def send_weekly_email(self):
        """Start this week's email if it is due; re-queue a stranded one.

        Hourly is the schedule's resolution, which is why the weekly email has
        a day and an hour in SiteConfig rather than a crontab line of its own:
        an instance changes when it sends in the admin, not on the VM.
        """
        from digest.sending import start_due_run

        run = start_due_run()
        if run is not None:
            self.stdout.write(f"  weekly email {run.send_date}")

    def forget_unconfirmed_signups(self):
        """Drop weekly-email signups nobody confirmed within 30 days.

        An unconfirmed row is an address someone typed — possibly not their
        own — and keeping it serves nothing once the link has expired.
        """
        from digest.services import forget_unconfirmed

        count = forget_unconfirmed()
        if count:
            self.stdout.write(f"  forgot {count} unconfirmed signup(s)")

    def tidy_sponsorships(self):
        """Close abandoned checkouts, release holds that ran out, warn first.

        The webhook does most of this as it happens; this is the backstop for
        a delivery that never arrived, and the only thing that notices a hold
        is about to run out, because nothing at Stripe's end happens then.
        """
        from sponsors.services import housekeeping

        done = housekeeping()
        if any(done.values()):
            self.stdout.write(
                "  sponsorships: {abandoned} abandoned, {lapsed} lapsed, "
                "{warned} warned, {settled} settled".format(**done)
            )
