"""Mark an account's email address as verified.

Email verification is mandatory, which applies to accounts created from the
shell too — a user made with `createsuperuser` has no confirmation email to
click and therefore cannot log in. This is the way out:

    python manage.py createsuperuser
    python manage.py verify_email you@example.com

The first administrator no longer needs either command — see `accounts.claim`,
which does both from a page in the browser. This remains the answer when a
moderator's confirmation mail bounces, or when someone genuinely wants to
build the first account before the site is reachable.
"""

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Mark an account's email address as verified so it can sign in."

    def add_arguments(self, parser):
        parser.add_argument("email", help="Email address of the account.")

    def handle(self, *args, **options):
        from allauth.account.models import EmailAddress

        email = options["email"]
        User = get_user_model()

        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            raise CommandError(f"No account with email {email!r}.")

        address, created = EmailAddress.objects.get_or_create(
            user=user,
            email__iexact=email,
            defaults={"email": email, "primary": True, "verified": True},
        )

        if not created:
            if address.verified:
                self.stdout.write(f"{email} was already verified.")
                return
            address.verified = True
            address.primary = True
            address.save(update_fields=["verified", "primary"])

        self.stdout.write(self.style.SUCCESS(f"{email} is now verified."))
