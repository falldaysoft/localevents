import hashlib
import secrets

from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """A registered submitter.

    Login is by email (see ACCOUNT_LOGIN_METHODS), so `email` carries the
    uniqueness constraint that `username` would normally have. `username` is
    still required, but only as the public display name shown against a
    listing — it is never used to authenticate.
    """

    email = models.EmailField("email address", unique=True)

    def __str__(self):
        return self.username or self.email

    @property
    def display_name(self):
        return self.username or self.email.split("@")[0]

    @property
    def is_moderator(self):
        return self.is_superuser or self.groups.filter(name="Moderators").exists()


class ApiTokenQuerySet(models.QuerySet):
    def active(self):
        return self.filter(revoked_at__isnull=True)


class ApiToken(models.Model):
    """A key an agent presents instead of a browser session.

    Only a hash is stored. The key is shown once, on the profile page that
    made it, and a leaked database backup then hands out nothing usable. The
    key is 32 random bytes, so a fast hash is enough — a slow one guards
    against guessing, and nobody is guessing this.

    A token acts as its owner and nothing more: it is read by the API views'
    own decorator, never by an authentication backend, so a moderator's token
    opens `/api/` and not `/moderate/`.
    """

    PREFIX = "le_"

    user = models.ForeignKey(
        "accounts.User", on_delete=models.CASCADE, related_name="api_tokens"
    )
    name = models.CharField(
        max_length=80, help_text="Which agent or script holds this key."
    )
    key_hash = models.CharField(max_length=64, unique=True)
    # Enough of the key to tell two tokens apart on the profile page.
    hint = models.CharField(max_length=12)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = ApiTokenQuerySet.as_manager()

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} ({self.hint}…)"

    @staticmethod
    def hash_key(key):
        return hashlib.sha256(key.encode()).hexdigest()

    @classmethod
    def issue(cls, user, name):
        """Make a token and return it with the only copy of its key."""
        key = cls.PREFIX + secrets.token_urlsafe(32)
        token = cls.objects.create(
            user=user,
            name=name.strip()[:80] or "Unnamed",
            key_hash=cls.hash_key(key),
            hint=key[: len(cls.PREFIX) + 6],
        )
        return token, key

    @classmethod
    def authenticate(cls, key):
        """The live token for this key, or None."""
        if not key or not key.startswith(cls.PREFIX):
            return None
        return (
            cls.objects.active()
            .select_related("user")
            .filter(key_hash=cls.hash_key(key), user__is_active=True)
            .first()
        )
