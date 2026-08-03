"""Periodic maintenance, run from the Helm CronJob.

One command rather than several so the schedule has a single entry point. Each
step is independent and failure-isolated: a broken import source must not stop
series expiry from running.
"""

import logging

from django.core.management.base import BaseCommand

logger = logging.getLogger("core.housekeeping")


class Command(BaseCommand):
    help = "Run periodic maintenance: poll import sources, expire stale series."

    def handle(self, *args, **options):
        steps = [
            ("poll import sources", self.poll_import_sources),
            ("expire stale series", self.expire_series),
            ("send renewal reminders", self.send_renewal_reminders),
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

    def poll_import_sources(self):
        return

    def expire_series(self):
        return

    def send_renewal_reminders(self):
        return
