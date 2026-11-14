"""Print the weekly email one reader would get now, without sending it.

For checking the content against real data before the sender exists, and
afterwards for answering "what did this person see?". An address with no
subscription is previewed as a fresh one — its account's marks, if it has an
account — and nothing is saved either way.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from digest.content import render_for
from digest.models import Subscriber, normalize_email


class Command(BaseCommand):
    help = "Render the weekly email for an address and print it."

    def add_arguments(self, parser):
        parser.add_argument("email")
        parser.add_argument(
            "--html", action="store_true", help="Print the HTML part, not the text."
        )

    def handle(self, email, html=False, **options):
        email = normalize_email(email)
        subscriber = Subscriber.objects.filter(email=email).first()
        if subscriber is None:
            user = get_user_model().objects.filter(email__iexact=email).first()
            subscriber = Subscriber(email=email, user=user, source="form")
        digest = render_for(subscriber)
        self.stderr.write(
            f"Subject: {digest.subject}\n"
            f"{digest.event_count} events, {digest.favorites_count} weekend picks\n"
        )
        self.stdout.write(digest.html if html else digest.text)
