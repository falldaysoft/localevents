"""Read the submissions mailbox. Run from crontab every few minutes.

Housekeeping runs this too, hourly, as a backstop — but an hour is a long
time for someone who just emailed an event to wait for "got it".
"""

from django.core.management.base import BaseCommand

from submissions import inbound


class Command(BaseCommand):
    help = "Fetch unseen mail from the submissions mailbox and queue triage."

    def handle(self, *args, **options):
        if not inbound.is_configured():
            self.stdout.write("inbound email is not configured (Mail configuration in the admin, or INBOUND_IMAP_HOST)")
            return
        stored = inbound.poll()
        self.stdout.write(f"stored {stored} new message(s)")
