"""Take one raw message on stdin, as if the mailbox had delivered it.

For trying triage locally against a saved .eml, and for an MTA that would
rather pipe mail to a command than be polled.
"""

import sys

from django.core.management.base import BaseCommand

from submissions import inbound


class Command(BaseCommand):
    help = "Read one RFC 822 message from stdin and queue it for triage."

    def handle(self, *args, **options):
        result = inbound.receive(sys.stdin.buffer.read())
        if result is None:
            self.stdout.write("already received; nothing to do")
        else:
            self.stdout.write(f"stored inbound email {result.pk}: {result.get_status_display()}")
